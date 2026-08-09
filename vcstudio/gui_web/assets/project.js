// project.js — 吸附能项目页:新建项目(清洁表面 + 构型族 + 气相参考 → 批量生成)
// + 已有项目 ΔE 汇总 / 导出 CSV / 生成完整报告。行为对齐 vcstudio/gui/project_tab.py。
// 只依赖 app.js 暴露的 VCS.* 与 api 桥方法(proj_*/pick_file/pick_dir)。
// 全部插值走 VCS.esc;零 emoji;中文文案。数字列用 td.num 右对齐等宽。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = v || ''; };
  const CURRENT_PROJECT_KEY = 'vcs.adsorption.current_project';
  const COMPARE_PROJECTS_KEY = 'vcs.adsorption.compare_projects';
  const SINGLE_REPORT_FORMATS = Object.freeze([
    {
      id: 'pj-report-format-html', value: 'html', label: 'HTML',
      description: '浏览器预览（始终可用）',
    },
    {
      id: 'pj-report-format-docx', value: 'docx', label: 'DOCX',
      description: '编辑与批注',
    },
    {
      id: 'pj-report-format-pdf', value: 'pdf', label: 'PDF',
      description: '打印与归档',
    },
  ]);
  const REPORT_CAPABILITY_PENDING_REASON =
    '正在等待后端明确确认；确认可用前不会提交此格式。';

  function initialReportCapabilities() {
    return {
      html: { available: true, reason: '' },
      docx: { available: false, reason: REPORT_CAPABILITY_PENDING_REASON },
      pdf: { available: false, reason: REPORT_CAPABILITY_PENDING_REASON },
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
    projects: [],     // proj_list 返回:[{path,name,n_members}]
    profiles: [],     // list_profiles 返回；一站式提交资源选择
    importRows: [],   // 本地结果扫描候选(前端只持有修正值;commit 时后端会重新验证)
    importResult: null,
    preparedLis: null, // {path,fingerprint,name,submitted}:生成成功后复用，安全重试提交
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
    workflowProjectPath: '',
    lisBusy: false,
    inputScanBusy: false,
    inputGeneration: 0,
    comparePaths: new Set(),
    compareSelectionRestored: false,
    comparePreview: null,
    comparePreviewGeneration: 0,
    comparePreviewTimer: null,
    compareFiguresBusy: false,
    batchReportBusy: false,
    candidateEvaluationGeneration: 0,
    reportDiagnostic: false,
    reportBusy: false,
    reportCapabilities: initialReportCapabilities(),
    reportCapabilityState: 'pending',
    reportCapabilityGeneration: 0,
  };

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
    clean_slab: '清洁表面', config: '吸附构型', gas_ref: '吸附质气相参考',
    molecule_ref: '锂硫 / 分子能量库', standalone: '独立计算结果', ignore: '不导入',
  };
  const TASK_LABELS = {
    auto: '自动识别', relax: '结构优化 relax', cellopt: '晶胞优化 cellopt',
    static: '静态能量 static', freq: '频率 freq', dos: '态密度 DOS',
    band: '能带 band', unknown: '尚未识别',
  };
  const DEFAULT_CONFIRMATION_REASON = '已核对原始 OUTCAR/OSZICAR 与末结构，确认该任务收敛';

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
    State.workflowProjectPath = String(project && project.path || '');
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
      if (status) status.textContent = '当前项目有需要处理的任务。打开任务页即可查看原因并续算或重新下载。';
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = '处理任务异常';
      setJourneyPrimary('ads-route-new');
    } else if (State.workflowResultReady || State.workflowStage === 'report_done') {
      if (status) status.textContent = '任务已完成，报告产物已生成；科学状态可能是最终、诊断或阻断，请在报告卡中核对。';
      const resultButton = $('ads-route-results');
      if (resultButton) resultButton.textContent = '查看 ΔE 与报告';
      setJourneyPrimary('ads-route-results');
    } else if (State.workflowAnalysisReady || State.workflowStage === 'analysis') {
      if (status) status.textContent = '整组任务已完成。下一步检查 ΔE；全部有效后即可生成报告。';
      const resultButton = $('ads-route-results');
      if (resultButton) resultButton.textContent = '检查 ΔE 并生成报告';
      setJourneyPrimary('ads-route-results');
    } else if (State.workflowSubmitted) {
      if (status) status.textContent = '整组任务已提交，自动托管正在监控、续算和下载关键结果。请保持软件运行。';
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = '查看任务进度';
      setJourneyPrimary('ads-route-new');
    } else if (State.workflowPendingSubmit) {
      if (status) status.textContent = '整组输入已生成，但仍有作业等待提交。请到任务页完成提交。';
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = '提交已生成作业';
      setJourneyPrimary('ads-route-new');
    } else if (refs.length) {
      if (status) status.textContent = `已找到 ${refs.length} 个可复用的参考能项目。下一步添加 slab 与 adsorption 结构。`;
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = '已有参考能，开始新的吸附计算';
      setJourneyPrimary('ads-route-new');
    } else {
      if (status) status.textContent = '还没有可复用的 Li-S 参考能。先导入你已经算好并收敛的 Li-S 化合物结果。';
      setJourneyPrimary('ads-route-import');
    }
  }

  function showImportProblem(message, fix) {
    const box = $('pj-import-problem');
    if (!box) return;
    if (!message) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML = `<b>${VCS.esc(message)}</b><span>怎么处理：${VCS.esc(fix || '按提示修正后重新检查。')}</span>`;
  }

  async function openProjectResults(projectPath) {
    await reloadProjects();
    const sel = $('pj-select');
    const hit = State.projects.find(p => p.path === projectPath || p.name === projectPath);
    if (sel && hit) {
      sel.value = hit.path;
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
    if (energy.value_eV != null) out.push(`能量 ${energy.value_eV} eV（${energy.source || '来源未知'}）`);
    if (electronic.final_scf_steps != null) {
      out.push(`末电子步 ${electronic.final_scf_steps}/${electronic.nelm || 'NELM 未知'}` +
        (electronic.nelm_saturated ? '，已达到上限' : '，未达到上限'));
    }
    if (ionic.converged_marker) out.push('OUTCAR 含结构优化收敛标志');
    if (ionic.final_force_max_eV_A != null) out.push(`末最大力 ${ionic.final_force_max_eV_A} eV/Å`);
    if (completion.outcar_footer) out.push('OUTCAR 含正常结束页脚');
    if (completion.vasprun_complete) out.push('vasprun.xml 结构完整');
    if (completion.soft_stopped) out.push('检测到 STOPCAR / soft stop');
    const cross = raw.cross_file_energy || {};
    const crossDelta = cross.spread_eV != null ? cross.spread_eV
      : cross.max_delta_eV != null ? cross.max_delta_eV
        : cross.max_difference_eV != null ? cross.max_difference_eV : cross.max_delta;
    if (cross.consistent === true) {
      out.push('跨文件末能量一致' +
        (crossDelta != null ? `（最大差 ${crossDelta} eV）` : '（OSZICAR / OUTCAR / vasprun.xml）'));
    } else if (cross.consistent === false) {
      out.push('跨文件末能量不一致' + (crossDelta != null ? `（最大差 ${crossDelta} eV）` : ''));
    }
    if (raw.fatal_error) out.push(`致命错误：${raw.fatal_error}`);
    return out.length ? out : textList(raw);
  }

  function actionLabel(value) {
    const action = String(value || '');
    return ({
      import_done: '无需额外处理，可直接导入',
      manual_confirm: '核对原始输出确已正常结束后，可勾选人工确认',
      repair_or_recalculate: '按硬性问题补齐输出或续算，然后重新检查',
      inspect_output: '打开原始输出核对结束状态，补齐文件后重新检查',
      submit_created: '四件套已就绪；导入后前往任务页选择服务器提交',
      submit: '四件套已就绪；导入后前往任务页选择服务器提交',
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
      name: String(c.name || pathBase(path) || `结果 ${index + 1}`),
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
    if (!selected.length) issue = '请至少勾选一个可导入结果';
    else if (missingConfirmationReasons.length) issue =
      `有 ${missingConfirmationReasons.length} 个人工确认结果尚未填写核对依据`;
    else if (unresolved.length) issue = `仍有 ${unresolved.length} 个已选结果需要处理`;
    else if (pendingMappings.length) issue = `请确认 ${pendingMappings.length} 个已选结果的智能角色/物种分组`;
    else if (clean > 1) issue = '一个吸附能项目只能选择 1 个清洁表面';
    else if (gas > 1) issue = '一个吸附能项目只能选择 1 个吸附质气相参考';
    else if (missingSpecies.length) issue = `有 ${missingSpecies.length} 个分子参考未填写物种名称（如 Li2S4、S8）`;
    else if (missingConfigSpecies.length) issue =
      `已选择逐物种分子参考：请为 ${missingConfigSpecies.length} 个吸附构型填写对应物种（如 Li2S8）`;
    else if (duplicateMoleculeSpecies.length) issue =
      `分子参考物种重复：${Array.from(new Set(duplicateMoleculeSpecies)).join('、')}`;
    else if (unmatchedConfigSpecies.length) issue =
      `有 ${unmatchedConfigSpecies.length} 个吸附构型没有同名分子参考`;
    else if (unsafeMolecules.length) issue =
      '分子参考仅接收 DONE 结果或已验证的完整四件套待提交；待核项请改为“独立计算结果”';
    else if (invalidTasks.length) issue = `有 ${invalidTasks.length} 个结果尚未选择受支持的任务类型`;
    else if (configs && clean !== 1) issue = '已选择吸附构型，请再指定 1 个清洁表面';
    else if (clean && !configs) issue = '已选择清洁表面，请至少再选择 1 个吸附构型';
    else if (gas && !clean) issue = '吸附质气相参考需与清洁表面和吸附构型一起导入';
    else if (!clean && !molecules && !standalone) issue = '请修正结果角色后再导入';
    return { ok: !issue, issue, selected, clean, configs, gas, molecules, standalone };
  }

  function renderImportSummary() {
    const box = $('pj-import-summary');
    const attention = $('pj-import-attention');
    if (!box || !attention) return;
    const n = importCounts();
    box.innerHTML = [
      ['total', n.total, '扫描到的计算目录'],
      ['ready', n.ready, '可直接导入'],
      ['created', n.created, '四件套待提交'],
      ['review', n.review, '需你确认'],
      ['blocked', n.blocked, '暂不可导入'],
    ].map(x => `<div class="pj-import-stat ${x[0]}"><b>${x[1]}</b><span>${x[2]}</span></div>`).join('');
    const gate = importMethodGate();
    const pending = n.review + n.blocked;
    attention.classList.toggle('warn', pending > 0 || !gate.ok);
    if (pending) {
      attention.textContent = `软件已先选中 ${n.ready - n.created} 个可靠结果` +
        (n.created ? `和 ${n.created} 个待提交四件套` : '') +
        '。请处理黄色/红色条目；点击每行“查看证据”可看到无法导入的具体原因。';
    } else if (n.created) {
      attention.textContent = `其中 ${n.created} 个目录只有完整四件套、尚未计算；会以“待提交”导入，随后前往任务页选择服务器提交，不会冒充已收敛结果。`;
    } else {
      attention.textContent = '所有结果均已通过检查。确认清洁表面和吸附构型角色后即可建立项目。';
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
      `${VCS.esc(labels[value] || value)}</option>`).join('');
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
      if (!group) { group = { key, role: row.role, species: row.species || '未识别', rows: [] }; groups.push(group); }
      group.rows.push(row);
    });
    groups.sort((left, right) => left.key.localeCompare(right.key));
    body.innerHTML = groups.map((group, groupIndex) => {
      const pending = group.rows.filter(row => row.mappingRequired && !row.mappingConfirmed);
      const groupHeader = '<tr class="pj-import-group-row"><td colspan="6"><span><b>' +
        `${VCS.esc(ROLE_LABELS[group.role] || group.role)}</b> / ${VCS.esc(group.species)} · ` +
        `${group.rows.length} 项</span>` + (pending.length
          ? `<button class="btn quiet" type="button" data-confirm-import-group="${groupIndex}">确认本组并选中</button>` : '') +
        '</td></tr>';
      const groupRows = group.rows.map(row => {
      const status = importStatus(row);
      const convergenceStatus = convergenceImportStatus(row);
      const created = isCreatedInput(row);
      const statusLabel = created ? '四件套完整，待提交'
        : { ready: '可导入', review: '需确认', blocked: '暂不可导入' }[status];
      const statusClass = created ? 'run'
        : { ready: 'ok', review: 'warn', blocked: 'fail' }[status];
      const reasons = [...row.blocking, ...row.diagnosis, ...row.warnings, ...row.evidence];
      const mappingPending = row.mappingRequired && !row.mappingConfirmed;
      const mainReason = (mappingPending ? `智能分组待确认：${row.roleReason || '请核对角色与物种'}` : '') ||
        row.blocking[0] || row.diagnosis[0] || row.warnings[0] ||
        (created ? '输入文件已通过检查；尚无输出，导入后需要提交计算'
          : status === 'ready' ? '能量与收敛证据已通过检查' : '尚缺少足够的完成证据');
      const detail = reasons.length ? '<details><summary>查看证据与完整原因</summary><ul class="pj-import-evidence">' +
        reasons.map(item => `<li>${VCS.esc(item)}</li>`).join('') + '</ul></details>' : '';
      const roleOptions = ['clean_slab', 'config', 'gas_ref', 'molecule_ref', 'standalone', 'ignore'];
      const taskOptions = ['unknown', 'relax', 'static', 'freq', 'dos', 'band'];
      const disabled = status === 'blocked' && !row.confirmationEligible ? ' disabled' : '';
      const speciesInput = ['config', 'molecule_ref'].includes(row.role)
        ? `<input class="ipt pj-import-species" data-act="species" value="${VCS.esc(row.species)}" ` +
          `placeholder="${row.role === 'molecule_ref' ? '物种，如 Li2S4' : '吸附物种，如 Li2S8（使用分子参考时必填）'}">`
        : '';
      const mappingHint = `<span class="pj-import-mapping ${mappingPending ? 'pending' : 'confirmed'}">` +
        `${mappingPending ? '待确认' : '已确认'} · ${VCS.esc(row.speciesSource || row.roleReason || '人工设置')}</span>`;
      const confirmationInput = row.confirmationEligible
        ? `<textarea class="ipt pj-import-confirm-reason" data-act="confirm-reason" rows="2" ` +
          `placeholder="请填写你核对了哪些输出证据">${VCS.esc(row.confirmationReason)}</textarea>`
        : '';
      const manualLabel = created ? '输入检查已通过，不是收敛结果'
        : convergenceStatus === 'ready' ? '自动检查已通过，无需人工确认'
        : row.confirmationEligible ? '我已核对并确认收敛' : '存在硬性问题，不能人工跳过';
      const manualHint = created ? '导入后在任务页选择服务器、核数和墙时再提交'
        : convergenceStatus === 'ready' ? '可直接导入；提交时仍会复核源文件是否变化'
        : row.confirmationEligible ? '请保留可审计的核对依据；提交时仍会复核硬性门禁'
          : '请按左侧原因补齐结果';
      return `<tr data-import-index="${row.index}" class="pj-import-${status}">` +
        `<td class="pj-import-check"><input type="checkbox" data-act="select"${row.selected ? ' checked' : ''}${disabled}></td>` +
        `<td class="pj-import-dir"><span class="name" title="${VCS.esc(row.path)}">${VCS.esc(row.name)}</span>` +
        `<span class="sub" title="${VCS.esc(row.path)}">${VCS.esc(row.path)}</span></td>` +
        `<td><select class="ipt" data-act="role">${optionHtml(roleOptions, row.role, ROLE_LABELS)}</select>${speciesInput}${mappingHint}</td>` +
        `<td><select class="ipt" data-act="task">${optionHtml(taskOptions, row.taskType, TASK_LABELS)}</select></td>` +
        `<td class="pj-import-reason"><span class="pill ${statusClass}"><i></i>${statusLabel}</span> ` +
        `<span class="pj-import-mainreason">${VCS.esc(mainReason)}</span>` +
        (row.action ? `<span class="pj-import-action">下一步：${VCS.esc(row.action)}</span>` : '') + detail + '</td>' +
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
          row.confirmationReason = DEFAULT_CONFIRMATION_REASON;
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
        ? `将导入 ${gate.selected.length} 个结果（清洁表面 ${gate.clean}、吸附构型 ${gate.configs}、分子参考 ${gate.molecules}、独立结果 ${gate.standalone}）`
        : gate.issue;
    }
    if (button) button.disabled = !gate.ok || !name || !root;
  }

  function renderImport() {
    renderImportSummary();
    renderImportRows();
    updateImportCommit();
  }

  async function chooseImportSource() {
    const r = await VCS.call('pick_dir');
    if (r && r.error) {
      VCS.log('选择结果文件夹失败:' + r.error, 'failc');
      showImportProblem('没有选中结果文件夹', '重新点击“选择整个文件夹”；应选择包含多个计算子目录的上层文件夹。');
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
      showImportProblem('尚未选择结果根文件夹', '点击“选择整个文件夹”，选择包含 OUTCAR / OSZICAR 等结果的上层目录。');
      VCS.toast('请先选择包含计算结果的根文件夹', 'fail');
      return;
    }
    const button = $('pj-import-scan');
    const review = $('pj-import-review');
    const done = $('pj-import-done');
    if (button) { button.disabled = true; button.textContent = '正在检查…'; }
    if (done) done.hidden = true;
    setImportStep(2);
    VCS.log('正在检查本地结果:' + source + '…');
    showImportProblem('', '');
    try {
      const r = await VCS.call('proj_import_scan', source);
      if (!r || r.ok === false || r.error) {
        VCS.log('结果检查失败:' + ((r && r.error) || '未知错误'), 'failc');
        showImportProblem('结果检查未完成：' + ((r && r.error) || '未知错误'),
          '确认目录仍可访问，并选择含 OUTCAR、OSZICAR 或 vasprun.xml 的上层文件夹后重新检查。');
        VCS.toast('没有完成检查，请查看日志中的具体原因', 'fail');
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
      VCS.log(`已检查 ${n.total} 个计算目录：${n.ready} 个可导入，${n.review} 个需确认，${n.blocked} 个暂不可导入`,
        n.blocked ? 'warnc' : 'okc');
      if (!n.total) {
        showImportProblem('没有发现可识别的 VASP 结果',
          '不要只选空的项目目录；请选择包含 OUTCAR、OSZICAR 或 vasprun.xml 的计算目录或其上层文件夹。');
        VCS.toast('没有发现可识别的 VASP 结果，请确认选择的是上层根文件夹', 'fail');
      }
    } finally {
      if (button) { button.disabled = false; button.textContent = '重新检查'; }
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

  async function commitImport() {
    const gate = importMethodGate();
    const source = val('pj-import-source'), root = val('pj-import-root');
    const name = val('pj-import-name');
    if (!gate.ok || !source || !root || !name) { updateImportCommit(); return; }
    const button = $('pj-import-commit');
    if (button) { button.disabled = true; button.textContent = '正在导入并复核…'; }
    VCS.log(`正在导入 ${gate.selected.length} 个本地结果并建立项目「${name}」…`);
    try {
      const r = await VCS.call('proj_import_commit', source, root, name, importSelections());
      if (!r || r.ok === false || r.error) {
        VCS.log('导入失败:' + ((r && r.error) || '未知错误'), 'failc');
        showImportProblem('导入未完成：' + ((r && r.error) || '未知错误'),
          '按黄色/红色条目的“下一步”修正；若目标项目已存在，请更换项目名或保存位置后重试。');
        textList(r && (r.errors || r.blocking_reasons)).forEach(x => VCS.log(x, 'warnc'));
        VCS.toast('导入未完成；扫描后结果可能有变化，请按提示重新检查', 'fail');
        return;
      }
      textList(r.warnings).forEach(x => VCS.log(x, 'warnc'));
      showImportProblem('', '');
      VCS.log('本地结果已建立吸附能项目:' + (r.project_path || name), 'okc');
      if (r.auto_report && r.auto_report.ok) {
        const files = (r.auto_report.files || [r.auto_report.file]).filter(Boolean);
        VCS.log((r.auto_report_reason ? '诊断报告' : '最终报告') +
          '已自动生成:' + files.join('；'), r.auto_report_reason ? 'warnc' : 'okc');
      } else if (r.auto_report && r.auto_report.error) {
        VCS.log('项目已导入，但自动报告暂未生成:' + r.auto_report.error, 'warnc');
      }
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      await reloadProjects();
      const sel = $('pj-select');
      const hit = State.projects.find(p => p.path === r.project_path || p.name === (r.project_name || name));
      if (sel && hit) {
        sel.value = hit.path;
        restoreWorkflowState(hit);
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
      const startWithReferences = () => startLiS(
        r.project_path || r.path || r.project_name || name);
      const openCreatedJobs = () => openImportedCreatedJobs(
        r.project_path || r.path || '');
      showImportDone(r, gate.selected.length, reportReady,
        gate.molecules > 0 && createdCount === 0,
        referenceOnly, createdCount, openCreatedJobs);
      setImportStep(4);
      if (!createdCount && isAdsorption && VCS.pipeline &&
          typeof VCS.pipeline.reconfigure === 'function') {
        await VCS.pipeline.reconfigure();
      }
      VCS.toast(createdCount ? `已导入；${createdCount} 个四件套作业等待提交`
        : referenceOnly ? 'Li-S 参考能库已建立，可以开始新的吸附计算'
        : reportReady ? '结果与 ΔE 已载入，报告将自动生成'
          : '结果已导入；将生成诊断报告并列出缺项');
      if (typeof VCS.nextStep === 'function') {
        VCS.nextStep({
          title: '结果导入完成',
          message: `已导入 ${gate.selected.length} 个条目并建立项目「${r.project_name || name}」。`,
          detail: createdCount
            ? `其中 ${createdCount} 个只有完整四件套、尚未运行。下一步到任务页选择服务器、核数和墙时后提交。`
            : referenceOnly
            ? '这些已收敛的 Li-S 能量已加入参考库。下一步只需选择固定 INCAR、clean slab 和 adsorption 结构。'
            : reportReady
            ? 'ΔE 已自动计算并显示在本页；最终报告将自动生成，也可立即打开报告入口。'
            : 'ΔE 表已自动刷新；系统将生成诊断报告并列出缺角色、缺能量或待确认结果。',
          primaryLabel: createdCount ? `提交 ${createdCount} 个待运行作业`
            : referenceOnly ? '用这些参考能开始吸附计算'
            : reportReady ? '查看或立即生成报告' : '查看 ΔE 缺项',
          stayLabel: createdCount ? '先检查待提交成员'
            : referenceOnly ? '先检查参考能清单' : '先检查导入清单',
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
      if (button) { button.textContent = '导入所选结果并建立项目'; updateImportCommit(); }
    }
  }

  async function openImportedCreatedJobs(projectPath) {
    const out = await VCS.navigate('jobs', { source: 'import-created-inputs' });
    if (!out.ok) return;
    if (window.Jobs && typeof window.Jobs.selectCreatedProject === 'function') {
      const selected = await window.Jobs.selectCreatedProject(projectPath);
      if (!selected) VCS.toast('未找到待提交成员，请在任务页清除筛选后检查', 'fail');
    }
  }

  function showImportDone(result, count, reportReady, hasSpeciesReferences, referenceOnly,
                          createdCount, openCreatedJobs) {
    const box = $('pj-import-done');
    if (!box) return;
    box.hidden = false;
    box.innerHTML = `<b>导入完成：</b>${VCS.esc(result.project_name || val('pj-import-name'))}，` +
      `共 ${count} 个条目。${createdCount ? `${createdCount} 个四件套作业等待提交。` : referenceOnly ? 'Li-S 参考能库已就绪。' : reportReady
        ? 'ΔE 已显示在下方，最终报告将自动生成。' : '将自动生成诊断报告并列出下方 ΔE 缺项。'}` +
      '<div class="actions">' + (referenceOnly ? ''
        : '<button class="btn" type="button" data-next="delta">重新计算 ΔE</button>') +
      (createdCount ? `<button class="btn primary" type="button" data-next="submit-created">提交 ${createdCount} 个待运行作业</button>` : '') +
      (hasSpeciesReferences
        ? '<button class="btn primary" type="button" data-next="lis">用这些参考能开始吸附计算</button>' : '') +
      (referenceOnly ? '' : `<button class="btn primary" type="button" data-next="report"${reportReady ? '' : ' disabled ' +
        'title="ΔE 或收敛状态仍有缺项，暂不生成最终报告"'}>查看或立即生成报告</button>`) + '</div>';
    const deltaButton = box.querySelector('[data-next="delta"]');
    if (deltaButton) deltaButton.addEventListener('click', delta);
    const submitCreated = box.querySelector('[data-next="submit-created"]');
    if (submitCreated && openCreatedJobs) submitCreated.addEventListener('click', openCreatedJobs);
    const lis = box.querySelector('[data-next="lis"]');
    if (lis) lis.addEventListener('click', () => startLiS(
      result.project_path || result.path || result.project_name || val('pj-import-name')));
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
    if (!task) { VCS.toast('请先选择要设置的任务类型', 'fail'); return; }
    const rows = selectedImportRows();
    if (!rows.length) { VCS.toast('请先勾选要修改的结果', 'fail'); return; }
    rows.forEach(row => { row.taskType = task; });
    renderImport();
  }

  function confirmSelectedImports() {
    const selected = selectedImportRows();
    const eligible = selected.filter(row => row.confirmationEligible);
    if (!selected.length) { VCS.toast('请先勾选要确认的结果', 'fail'); return; }
    if (!eligible.length) {
      VCS.toast('所选结果没有可人工确认项；请按行内原因补齐文件或重新计算', 'fail');
      return;
    }
    eligible.forEach(row => { row.manualConfirm = true; });
    renderImport();
    const refused = selected.length - eligible.length;
    VCS.toast(`已标记 ${eligible.length} 项；${refused ? `${refused} 项硬性问题未被跳过` : '提交时仍会重新复核'}`);
  }

  async function openImport(sourceRoot) {
    // 公开入口可从仪表盘直接调用；旧调用方曾传过模式名 "results"，它不是路径。
    if (sourceRoot === 'results') sourceRoot = '';
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
    if (r && r.error) { VCS.log('选择文件失败:' + r.error, 'failc'); return ''; }
    if (r && r.path) { setVal(id, r.path); return r.path; }
    return '';
  }
  async function pickDirInto(id) {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log('选择目录失败:' + r.error, 'failc'); return; }
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
    const path = val('lis-reference');
    return State.projects.find(p => p.path === path) || null;
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
        ? [`文件夹名同时匹配 ${matches.join('、')}，没有自动选择`] : [],
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
      ? `${value.old == null ? '缺失' : value.old} → ${value.new == null ? '已补齐' : value.new}` : '';
    const reason = textList(value.reason || value.message || value.description).join('；');
    return [target, change, reason].filter(Boolean).join('：');
  }

  function renderMethodCheck(check, legacyNeedsReview) {
    const box = $('lis-method-check');
    if (!box) return;
    if (!check && legacyNeedsReview) {
      check = {
        execution_status: 'ready', comparability_status: 'unverified',
        warnings: ['方法可比性证据尚未完整；作业可提交，自动 ΔE 与最终报告暂停'],
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
      ? '输入执行检查未通过'
      : comparabilityStatus === 'incompatible'
        ? '作业可提交；方法不一致会暂停自动 ΔE 与最终报告'
        : comparabilityStatus === 'unverified'
          ? '作业可提交；方法证据待核验'
          : '作业可提交；方法可比性已核验';
    const outcome = $('lis-method-outcome');
    if (outcome) {
      const submitText = executionStatus === 'blocked'
        ? '作业生成 / 提交：已阻止'
        : '作业生成 / 提交：可继续';
      const analysisText = executionStatus === 'blocked'
        ? '自动 ΔE / 最终报告：尚未运行，请先修正本目录输入'
        : comparabilityStatus === 'verified'
          ? '自动 ΔE / 最终报告：可继续'
          : comparabilityStatus === 'incompatible'
            ? '自动 ΔE / 最终报告：已暂停，需重算或修正方法差异'
            : '自动 ΔE / 最终报告：待补齐证据后继续';
      outcome.innerHTML = `<span>${VCS.esc(submitText)}</span><span>${VCS.esc(analysisText)}</span>`;
    }
    renderMethodSection('lis-method-issues', '影响 ΔE / 报告的问题（不阻止作业提交）', issues);
    renderMethodSection('lis-method-notes', '体系说明（包括 ISPIN）', notes);
    renderMethodSection('lis-method-warnings', '需留意', warnings);
    renderMethodSection('lis-method-repairs', '已在受管副本安全修复（源文件未改）', repairs,
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
    box.innerHTML = '<div class="lis-repair-title"><b>智能修复预览</b>' +
      '<span>只有低风险项可自动写入受管副本；源目录四件套保持原字节不变。' +
      (reviewActions.length ? ' MAGMOM、ISPIN 等科学选择只给建议，不自动改。' : '') + '</span></div>' +
      '<table><thead><tr><th>成员</th><th>参数</th><th>原值</th><th>建议值</th><th>处理</th><th>原因</th></tr></thead><tbody>' +
      rows.map(action => `<tr><td>${VCS.esc(action.member || '')}</td>` +
        `<td>${VCS.esc(action.key || '')}</td><td>${VCS.esc(action.old == null ? '缺失' : action.old)}</td>` +
        `<td>${VCS.esc(action.new)}</td><td>${String(action.risk || '').toLowerCase() === 'low'
          ? '低风险，可修复副本' : '仅建议，不自动'}</td>` +
        `<td>${VCS.esc(action.reason || '')}</td></tr>`).join('') +
      '</tbody></table><div class="actions">' +
      (safeActions.length
        ? `<button class="btn primary" type="button" data-lis-repair="apply"${current === 'apply' ? ' disabled' : ''}>仅修复 ${safeActions.length} 个低风险副本项并继续</button>` +
          `<button class="btn" type="button" data-lis-repair="keep"${current === 'keep' ? ' disabled' : ''}>保持各目录原样继续</button>`
        : '<span class="sub">这些是科学设置建议，不会自动修改，也不阻止提交。</span>') +
      '</div>';
    box.querySelectorAll('[data-lis-repair]').forEach(button => {
      button.addEventListener('click', () => {
        State.repairDecision = { plan_id: plan.plan_id, mode: button.dataset.lisRepair };
        VCS.log(button.dataset.lisRepair === 'apply'
          ? '已确认：仅低风险项修复到受管项目副本；MAGMOM/ISPIN 只建议，源目录不改'
          : '已确认：保持每个目录的原始四件套提交；最终 ΔE 仍受方法门禁约束', 'warnc');
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
    State.preparedConflictHint = { oldName: prepared.name, path: prepared.path };
    State.methodCheck = null;
    State.methodCheckFingerprint = '';
    State.repairPlan = null;
    State.repairDecision = null;
    const methodBox = $('lis-method-check');
    if (methodBox) methodBox.hidden = true;
    showLisFailure('输入已改变，不能复用刚才生成的项目',
      `原项目仍安全保留在 ${prepared.path}。如果要按新输入再生成，请把项目名改成新名称；不要覆盖旧项目。`,
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
      box.textContent = `将使用同目录 INCAR：${local.path}`;
    } else if (local.status && local.status !== 'missing') {
      box.className = 'sub';
      box.textContent = `clean slab INCAR 不可用：${textList(local.issues).join('；') || local.status}`;
    } else if (val('pj-incar')) {
      box.className = 'sub';
      box.textContent = `同目录无 INCAR，将使用你显式选择的备用文件：${val('pj-incar')}`;
    } else {
      box.className = 'sub';
      box.textContent = 'clean slab 同目录尚未找到 INCAR。';
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
      summary = `四件套不可提交：${quartet.issues.join('；') || '输入文件无效或冲突'}`;
    } else if (copyMode) {
      summary = '完整四件套原样绑定（源文件不改）';
    } else if (generateMode || quartet.hasEvidence) {
      const generated = missing.length ? missing.join('、') : '无';
      summary = `不完整输入（缺 ${generated}）：将以本目录 POSCAR+INCAR 生成受管四件套；源目录不改`;
    }
    if (!present.includes('INCAR') && fallbackIncar && !blocked) {
      fileRows.push(`INCAR 备用：${fallbackIncar}`);
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
      VCS.toast('当前输入仍在扫描、生成或提交，请完成后再导入新组', 'fail');
      return;
    }
    const picked = await VCS.call('pick_dir');
    if (picked && picked.error) {
      VCS.log('选择本次计算文件夹失败:' + picked.error, 'failc');
      showBundleStatus('bad', '没有选中文件夹。请选择包含 clean slab 与 adsorption 各成员输入的上层目录。');
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
    if (button) { button.disabled = true; button.textContent = '正在识别整套输入…'; }
    showBundleStatus('', '正在只读扫描 clean slab、adsorption 结构及各自 POSCAR/INCAR/KPOINTS/POTCAR…');
    try {
      const result = await VCS.call(
        'proj_scan_lis_inputs', picked.path, selectedReferenceSpecies());
      if (generation !== State.inputGeneration) return;
      if (!result || result.ok === false || result.error) {
        const message = (result && result.error) || '未知错误';
        VCS.log('识别本次计算文件夹失败:' + message, 'failc');
        showBundleStatus('bad', '识别失败：' + message + '。原始文件没有被修改，请修正目录后重试。');
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
      const summary = `已识别 ${result.clean_slab ? 'clean slab，' : ''}新增 ${added} 个 adsorption 结构，` +
        `${readyIncars + (State.cleanIncar.status === 'ready' ? 1 : 0)} 个成员已绑定本目录 INCAR。` +
        (quartetMembers.length
          ? ` 完整四件套原样绑定 ${copiedQuartets} 组，受管副本智能补齐 ${generatedQuartets} 组。` : '') +
        (rootFallback ? ` 根目录 INCAR 已作为显式备用：${rootFallback}。` : '');
      showBundleStatus(warnings.length ? 'warn' : 'ok', summary +
        (warnings.length ? ' 还需确认：' + warnings.join('；') : ' 请检查物种映射后继续。'));
      warnings.forEach(message => VCS.log(message, 'warnc'));
      VCS.log(`整套输入识别完成：${summary}`, warnings.length ? 'warnc' : 'okc');
    } finally {
      if (generation === State.inputGeneration) {
        State.inputScanBusy = false;
        syncLisInputLocks();
        updateLisReadiness();
        if (button) {
          button.disabled = false;
          button.textContent = '导入本次计算文件夹（推荐）';
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
    if (State.inputScanBusy) issue = '第 2 步：正在扫描并核对新输入组，请稍候';
    else if (!ref) issue = '第 1 步：请选择已经导入的 Li-S 参考能项目';
    else if (!refs.length) issue = '第 1 步：所选项目没有可用的 Li-S 物种参考能';
    else if (!val('pj-slab')) issue = '第 2 步：请选择 clean slab POSCAR';
    else if (cleanQuartetBlocked) issue = '第 2 步：clean slab 的四件套无效或冲突，请按成员明细修正';
    else if (!cleanIncarReady) issue = State.cleanIncar.status === 'missing'
      ? '第 2 步：clean slab 同目录缺少 INCAR；补齐文件或显式选择备用 INCAR'
      : `第 2 步：clean slab 的 INCAR 不可用（${textList(State.cleanIncar.issues).join('；') || State.cleanIncar.status}）`;
    else if (!items.length) issue = '第 2 步：至少添加一个 adsorption POSCAR';
    else if (invalidIncars.length) issue =
      `第 2 步：有 ${invalidIncars.length} 个构型的同目录 INCAR 冲突或无效`;
    else if (missingIncars.length) issue =
      `第 2 步：有 ${missingIncars.length} 个构型缺少同目录 INCAR；补齐文件或显式选择备用 INCAR`;
    else if (blockedQuartets.length) issue =
      `第 2 步：有 ${blockedQuartets.length} 个构型的四件套无效或冲突`;
    else if (invalidSpecies.length) issue = `第 2 步：有 ${invalidSpecies.length} 个构型的物种不在参考能集合中`;
    else if (unconfirmedSpecies.length) issue =
      `第 2 步：请确认 ${unconfirmedSpecies.length} 个构型的智能物种分组`;
    else if (!val('pj-name')) issue = '第 3 步：填写项目名';
    else if (!val('pj-root')) issue = '第 3 步：选择输出根目录';
    else if (nameConflict) issue = `第 3 步：旧项目“${State.preparedConflictHint.oldName}”已存在；请改用新项目名`;
    else if (!val('lis-profile')) issue = '第 4 步：选择用于提交的服务器';
    else if (!Number.isInteger(cores) || cores < 1) issue = '第 4 步：核数必须是正整数';
    else if (!validWalltime) issue = '第 4 步：墙时请填写为 HH:MM:SS（例如 24:00:00）';
    else if (methodBlocked) issue = '输入执行检查已阻止生成 / 提交；请按方法面板中的输入错误修正';
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
        ? `本地项目已经生成。直接重试提交即可，不会重复生成或覆盖：${prepared.path}`
        : gate.ok
          ? analysisPaused
            ? `作业已就绪，可在 ${val('lis-profile')} 提交；自动 ΔE / 最终报告将暂停，等方法证据修正或补齐后继续。`
            : `已就绪：将生成 clean slab + ${gate.items.length} 个 adsorption 作业，并在 ${val('lis-profile')} 提交。`
          : gate.issue;
    }
    const button = $('pj-submit-all');
    if (button) {
      const prepared = reusablePreparedLis();
      button.disabled = State.lisBusy || State.inputScanBusy || !gate.ok ||
        !!(prepared && prepared.submitted);
      if (!State.lisBusy) {
        button.textContent = prepared && prepared.submitted
          ? '整组已提交，自动托管运行中'
          : prepared ? '重试未提交成员（不会重复生成）'
            : ['unverified', 'incompatible'].includes(gate.methodStatus)
              ? '生成并提交整组（ΔE / 报告待核验）'
              : '生成并提交整组，开启自动续算/下载/报告';
      }
    }
    const status = $('lis-reference-status');
    if (status) {
      status.classList.toggle('ready', !!gate.ref && gate.refs.length > 0);
      status.textContent = gate.ref && gate.refs.length
        ? `已找到 ${gate.refs.length} 个参考物种：${gate.refs.join('、')}。每个 adsorption 构型必须从中选择。`
        : gate.ref ? '这个项目没有可用的物种参考能，请先导入已收敛的 Li-S 化合物结果。'
          : '请选择包含 Li-S 分子参考能的已导入项目。';
    }
    updateJourney();
  }

  function openLisStep(step) {
    const gate = lisGate();
    const firstIncomplete = [1, 2, 3, 4].find(value => !gate.step[value]) || 4;
    if (step > firstIncomplete) {
      VCS.toast(`请先完成第 ${firstIncomplete} 步；完成后下一步会自动展开`, 'fail');
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
    sel.innerHTML = `<option value="">${refs.length ? '请选择 Li-S 参考能项目' : '暂无已导入的 Li-S 参考能项目'}</option>` + refs.map(p => {
      const species = referenceSpecies(p);
      const count = species.length || Number(p.n_species_refs || 0);
      const detail = species.length ? `：${species.join('、')}` : '';
      return `<option value="${VCS.esc(p.path)}">${VCS.esc(p.name || pathBase(p.path))}（${count} 个物种${VCS.esc(detail)}）</option>`;
    }).join('');
    const hit = refs.find(p => p.path === previous || p.name === previous) ||
      (!previous && refs.length === 1 ? refs[0] : null);
    if (hit) sel.value = hit.path;
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
    select.innerHTML = '<option value="">选择物种后批量应用…</option>' + values.map(value =>
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
      ? `物种已确认 ${matched}/${State.configs.length}，INCAR 已绑定 ${incars}/${State.configs.length}` +
        (copiedQuartets || generatedQuartets
          ? `；四件套原样 ${copiedQuartets}，受管副本补齐 ${generatedQuartets}` : '')
      : '尚未添加构型';
    updateBulkSpeciesOptions(refs);
    if (!box) return;
    if (!State.configs.length) {
      box.innerHTML = '<div class="pj-cfgempty">尚未添加 adsorption POSCAR；可逐个添加，也可一次扫描整个文件夹。</div>';
      updateLisReadiness();
      return;
    }
    const onlyUnmatched = !!($('lis-only-unmatched') && $('lis-only-unmatched').checked);
    const rows = State.configs.map((path, index) => ({ path, index }))
      .filter(item => !onlyUnmatched || !confirmedSpecies(item.path) ||
        !incarReady(item.path) || quartetBlocked(State.configSpeciesMeta[item.path]));
    if (!rows.length) {
      box.innerHTML = '<div class="pj-cfgempty">所有构型的物种映射都已确认。</div>';
      updateLisReadiness();
      return;
    }
    const groups = [];
    rows.forEach(row => {
      const species = String(State.configSpecies[row.path] || '').trim();
      const key = species || '未识别';
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
        `${group.rows.length} 个构型 · ${confirmed} 个已确认</span>` +
        (canConfirm ? `<button class="btn quiet" type="button" data-confirm-group="${groupIndex}">确认本组映射</button>` : '') +
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
            ? `本目录无 INCAR；将使用显式备用：${val('pj-incar')}`
            : `INCAR 不可用：${textList(meta.incarIssues).join('；') || '同目录缺失'}`;
        const inputEvidence = quartet.hasEvidence
          ? `<div class="lis-quartet ${quartet.blocked ? 'blocked' : quartet.mode}">${quartetHtml(quartet)}</div>`
          : `<span class="sub" title="${VCS.esc(incarText)}">${VCS.esc(incarText)}</span>`;
        const sourceLabel = meta.source === 'poscar_minus_clean_slab' ||
          String(meta.source || '').startsWith('poscar_minus_clean_slab+')
          ? 'POSCAR−clean slab 组成差'
          : meta.source === 'user' ? '人工选择' : meta.source === 'path_name' ? '文件夹/文件名建议' : '未识别';
        const speciesCheck = confirmedRow ? `已确认：${sourceLabel}`
          : valid ? `自动建议 ${species}，待确认（${sourceLabel}）`
            : `未匹配参考能；请选择：${refs.join('、') || '先选参考项目'}`;
        const speciesOptions = '<option value="">请选择对应物种</option>' +
          (!valid && species ? `<option value="${VCS.esc(species)}" selected>${VCS.esc(species)}（未匹配）</option>` : '') +
          refs.map(value => `<option value="${VCS.esc(value)}"${value.toLowerCase() === species.trim().toLowerCase() ? ' selected' : ''}>${VCS.esc(value)}</option>`).join('');
        return `<div class="pj-cfgrow lis-cfgrow${quartet.blocked ? ' invalid' :
          confirmedRow && memberIncarReady ? '' : valid && memberIncarReady ? ' pending' : ' invalid'}">` +
          `<div class="lis-cfgpath"><span class="path" title="${VCS.esc(p)}">${VCS.esc(pathBase(p))}</span>` +
          `<span class="sub" title="${VCS.esc(p)}">${VCS.esc(p)}</span>` +
          `${inputEvidence}</div>` +
          `<label>对应物种<select class="ipt lis-species" data-species-index="${i}">${speciesOptions}</select></label>` +
          `<span class="lis-species-check">${VCS.esc(speciesCheck)}</span>` +
          `<button class="btn quiet" type="button" data-rm="${i}">移除</button></div>`;
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
        VCS.toast(`已确认 ${changed} 个 ${group.key} 构型的物种映射`);
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
      VCS.toast('请先从参考物种中选择一个值', 'fail');
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
    VCS.toast(changed ? `已为 ${changed} 个未确认构型绑定并确认 ${species}` : '所有构型已经确认，无需修改');
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
    if (r && r.error) { VCS.log('选择构型失败:' + r.error, 'failc'); return; }
    if (r && r.path) {
      const incar = await resolveMemberIncar(r.path);
      if (generation !== State.inputGeneration || lisInputsLocked()) return;
      if (!appendConfig(r.path, '', incar)) {
        VCS.log('该构型已在列表中，已跳过:' + r.path);
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
      VCS.log('选择构型文件夹失败:' + picked.error, 'failc');
      showConfigScanStatus('bad', '没有选中结构文件夹。请重新选择包含 POSCAR / CONTCAR / .vasp 文件的上层目录。');
      return;
    }
    if (!picked || !picked.path) return;
    if (!val('pj-name')) setVal('pj-name', pathBase(picked.path) + '_adsorption');
    if (!val('pj-root')) setVal('pj-root', pathParent(picked.path));
    const button = $('pj-cfg-dir');
    State.inputScanBusy = true;
    syncLisInputLocks();
    updateLisReadiness();
    if (button) { button.disabled = true; button.textContent = '正在扫描结构…'; }
    showConfigScanStatus('', '正在递归寻找 POSCAR / CONTCAR / .vasp 结构文件…');
    VCS.log('正在递归扫描 adsorption POSCAR:' + picked.path + '…');
    try {
      const r = await VCS.call(
        'proj_scan_structures', picked.path, val('pj-slab'), selectedReferenceSpecies());
      if (generation !== State.inputGeneration) return;
      if (!r || r.ok === false || r.error) {
        VCS.log('扫描结构文件夹失败:' + ((r && r.error) || '未知错误'), 'failc');
        showConfigScanStatus('bad', '扫描失败：' + ((r && r.error) || '未知错误') +
          '。请确认目录可访问，并选择包含结构文件的上层目录。');
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
      VCS.log(`结构扫描完成：新增 ${added} 个，重复或无效跳过 ${raw.length - added} 个`, added ? 'okc' : 'warnc');
      if (!raw.length) {
        showConfigScanStatus('bad', '没有找到结构文件。请确认文件名为 POSCAR / CONTCAR，或扩展名为 .vasp / .poscar。');
        VCS.toast('没有找到 POSCAR / CONTCAR 结构文件', 'fail');
      } else {
        const choice = preferred.suppressed
          ? `；同目录冲突时已优先选择本次输入 POSCAR（跳过 ${preferred.suppressed} 个重复文件）` : '';
        const finalNote = preferred.finalStructures
          ? `；${preferred.finalStructures} 个目录仅能回退使用 CONTCAR，请确认不是旧结果残留` : '';
        showConfigScanStatus('ok', `已新增 ${added} 个结构${choice}${finalNote}。请逐行确认“对应物种”，红色项必须修正后才能提交。`);
      }
    } finally {
      if (generation === State.inputGeneration) {
        State.inputScanBusy = false;
        syncLisInputLocks();
        updateLisReadiness();
        if (button) { button.disabled = false; button.textContent = '导入整个结构文件夹'; }
      }
    }
  }

  function updateLisResourceSummary() {
    const box = $('lis-resource-summary');
    if (!box) return;
    const profile = State.profiles.find(item => item.name === val('lis-profile'));
    if (!profile) {
      box.textContent = '选择服务器后会自动带入该服务器的默认核数与墙时；本项目只绑定所选服务器。';
      return;
    }
    const queue = profile.queue ? `，队列 ${profile.queue}` : '';
    box.textContent = `本项目将提交到 ${profile.name}${queue}：每个作业 ${val('lis-cores') || '—'} 核，` +
      `墙时 ${val('lis-walltime') || '—'}。其他项目可同时选择别的服务器，软件会并行监控。`;
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
    if (r && r.error) VCS.log('读取服务器列表失败:' + r.error, 'failc');
    const previous = sel.value;
    let saved = '';
    try { saved = localStorage.getItem('vcs.jobs.profile') || ''; } catch (e) { /* 不阻塞 */ }
    if (!State.profiles.length) {
      sel.innerHTML = '<option value="">尚未配置服务器</option>';
      updateLisReadiness();
      return;
    }
    sel.innerHTML = State.profiles.map(p =>
      `<option value="${VCS.esc(p.name)}">${VCS.esc(p.name)}</option>`).join('');
    const wanted = [previous, saved].find(x => State.profiles.some(p => p.name === x));
    sel.value = wanted || State.profiles[0].name;
    applyLisProfileDefaults();
  }

  async function submitLiSProject(projectPath, gate) {
    let password = null;
    let trust = false;
    for (let attempt = 0; attempt < 6; attempt++) {
      const r = await VCS.call('submit_project_with_resources', projectPath,
        val('lis-profile'), gate.cores, gate.walltime, password, trust);
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
          error: '服务器主机指纹未通过核对。请到“集群”页测试连接后再重试。' };
        trust = pin;
        continue;
      }
      return r;
    }
    return { ok: false, error: '密码或主机信任重试次数过多，请检查服务器配置' };
  }

  function projectPathFrom(result) {
    return String(result && (result.project_path || result.path ||
      (result.project && (result.project.path || result.project.project_path))) || '');
  }

  function submissionRows(result) {
    const raw = result && (result.results || result.submissions || result.jobs || result.items) || [];
    const rows = raw.map((item, index) => {
      if (Array.isArray(item)) {
        return { name: pathBase(item[0] || `作业 ${index + 1}`), ok: item.length > 2 ? !!item[1] : true,
          message: item.length > 2 ? item[2] : item[1] };
      }
      const row = item || {};
      const state = String(row.state || row.status || '').toUpperCase();
      return {
        name: row.name || pathBase(row.dir || row.job_dir || row.path || `作业 ${index + 1}`),
        ok: row.ok != null ? !!row.ok : !['FAILED', 'ERROR', 'BLOCKED'].includes(state),
        message: row.message || row.error || row.job_id || state || '已提交',
      };
    });
    (result && result.skipped || []).forEach((item, index) => {
      const row = item || {};
      rows.push({
        name: row.name || pathBase(row.dir || row.job_dir || row.path || `跳过项 ${index + 1}`),
        ok: true,
        skipped: true,
        message: row.reason || row.message || '状态无需重复提交',
      });
    });
    return rows;
  }

  function lisRepairHint(message, stage) {
    const text = String(message || '');
    if (/已存在|不会覆盖/.test(text)) return '目标项目已经存在。请回到第 3 步换一个项目名，旧项目不会被覆盖。';
    if (/方法|核验|不能直接相减|可比性/.test(text)) return '作业输入可继续生成和提交；这些差异只会暂停自动 ΔE 与最终报告。请查看方法面板，按具体能量项补齐证据或重算；ISPIN 体系说明无需勾选确认。';
    if (/INCAR|POSCAR|结构文件|文件不存在|无法读取/.test(text)) return '文件路径已经失效或不可读。回到第 2 步重新选择对应文件。';
    if (/物种|species|参考/.test(text)) return '回到第 1、2 步：确认参考能项目，并把每个红色构型改为参考集合中的物种。';
    if (/POTCAR|赝势/.test(text)) return '到“设置”中配置可用的 POTCAR 库，再返回重试；不要手工拼接不一致的赝势。';
    if (/凭据.*保存|keyring|无人值守|自动(?:托管|驾驶).*保存/i.test(text)) {
      return '任务已经提交，不要重新生成项目。请到“集群”页重新保存可供无人值守使用的凭据，再回来重试接管。';
    }
    if (/密码|认证|credential/i.test(text)) return '再次点击“重试未提交成员”，重新输入正确密码；已经生成的本地项目会直接复用。';
    if (/主机|指纹|host/i.test(text)) return '核对服务器指纹；确认无误后再次提交并选择信任。';
    if (/集群|服务器|profile|队列/.test(text)) return '回到第 4 步选择有效服务器；如列表为空，点击“配置服务器”。';
    return stage === 'prepare'
      ? '按上面的错误回到对应步骤修正；若刚才已经生成过同名项目，请使用新项目名。'
      : '本地项目已经保留。修正服务器、密码或资源后，再点“重试未提交成员”；成功项不会重复提交。';
  }

  function lisRepairAction(message, stage) {
    const text = String(message || '');
    if (/方法|核验|不能直接相减|可比性/.test(text)) return ['method', '查看 ΔE / 报告门禁'];
    if (/已存在|不会覆盖/.test(text)) return ['step3', '返回修改项目名'];
    if (/参考项目|参考能/.test(text)) return ['step1', '返回选择参考能'];
    if (/INCAR|POSCAR|结构文件|物种|species/.test(text)) return ['step2', '返回检查输入'];
    if (/主机|指纹|集群|服务器|profile|密码|认证|凭据|keyring/i.test(text)) return ['cluster', '前往集群配置'];
    return stage === 'submit' ? ['retry', '修正后重试未提交成员'] : ['step1', '返回逐步检查'];
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
      (reusable ? `<span>${VCS.esc(reusable.path)}</span>` : '') + '</div>' +
      `<div class="lis-result-error">${VCS.esc(message || '未知错误')}</div>` +
      `<div class="lis-result-fix"><b>怎么处理</b><span>${VCS.esc(lisRepairHint(message, stage))}</span>` +
      `<button class="btn" type="button" data-lis-fix="${action[0]}">${VCS.esc(action[1])}</button></div>`;
    bindLisRepairAction(box);
  }

  function renderLisSubmitResult(prepared, submitted) {
    const box = $('lis-submit-result');
    if (!box) return;
    const rows = submissionRows(submitted);
    const projectPath = projectPathFrom(prepared);
    const ok = submitted && submitted.ok !== false && !submitted.error;
    const automationNotReady = !ok && /凭据.*保存|keyring|无人值守|自动(?:托管|驾驶).*保存/i.test(
      String(submitted && submitted.error || '')) && rows.some(row => row.ok && !row.skipped);
    const heading = ok ? '整组已生成并提交'
      : automationNotReady ? '任务已提交，但自动续算尚未接管' : '项目已生成，但提交没有全部完成';
    let html = `<div class="lis-result-head ${ok ? 'ok' : 'bad'}"><b>${heading}</b>` +
      `<span>${VCS.esc(projectPath || val('pj-name'))}</span></div>`;
    if (rows.length) {
      html += '<table><thead><tr><th>成员作业</th><th>提交结果</th><th>说明 / 作业号</th></tr></thead><tbody>';
      rows.forEach(row => {
        html += `<tr><td>${VCS.esc(row.name)}</td><td>${row.skipped ? '<span class="lis-submit-skip">已跳过</span>' : row.ok ? VCS.pill('SUBMITTED') : VCS.pill('FAILED')}</td>` +
          `<td class="sub">${VCS.esc(row.message || '')}</td></tr>`;
      });
      html += '</tbody></table>';
    }
    if (submitted && submitted.error) html += `<div class="lis-result-error">${VCS.esc(submitted.error)}</div>`;
    if (!ok) {
      const action = lisRepairAction(submitted && submitted.error, 'submit');
      html += `<div class="lis-result-fix"><b>怎么处理</b><span>${VCS.esc(lisRepairHint(submitted && submitted.error, 'submit'))}</span>` +
        `<button class="btn" type="button" data-lis-fix="${action[0]}">${VCS.esc(action[1])}</button></div>`;
    }
    if (ok) {
      html += '<div class="lis-result-next"><b>自动托管已开启</b><span>请保持软件运行。软件会监控队列，未收敛时最多续算 3 轮，' +
        '每轮轻量拉回 OUTCAR、OSZICAR、CONTCAR，全部完成后自动生成报告。</span></div>' +
        '<div class="actions lis-result-actions"><button class="btn primary" type="button" data-lis-next="jobs">查看任务进度</button>' +
        '<button class="btn" type="button" data-lis-next="results">查看项目 ΔE 与报告</button></div>';
    }
    box.innerHTML = html;
    box.hidden = false;
    bindLisRepairAction(box);
    const jobs = box.querySelector('[data-lis-next="jobs"]');
    if (jobs) jobs.addEventListener('click', () => {
      if (typeof VCS.navigate === 'function') VCS.navigate('jobs', { source: 'lis-submitted' });
    });
    const results = box.querySelector('[data-lis-next="results"]');
    if (results) results.addEventListener('click', () => openProjectResults(projectPath));
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
    if (button) { button.disabled = true; button.textContent = reusable ? '正在重试未提交成员…' : '正在生成整组输入…'; }
    if (resultBox) resultBox.hidden = true;
    VCS.log(reusable
      ? '复用已生成项目，直接重试尚未提交的成员：' + reusable.path
      : `正在按 ${gate.refs.length} 个 Li-S 参考物种准备 clean slab + ${gate.items.length} 个 adsorption 作业…`);
    try {
      let prepared;
      if (reusable) {
        prepared = { ok: true, project_path: reusable.path, warnings: [], advisories: [] };
      } else {
        prepared = await VCS.call('proj_prepare_lis', val('pj-name'), val('pj-slab'), gate.items,
          val('pj-incar'), val('pj-root'), gate.ref.path, gate.methodConfirmation,
          gate.memberIncars, gate.repairRequest);
        const methodCheck = prepared && prepared.method_check;
        const needsMethod = !!(prepared && prepared.needs_method_confirmation);
        const needsRepair = !!(prepared && prepared.needs_repair_decision);
        if (methodCheck || needsMethod) renderMethodCheck(methodCheck, needsMethod);
        if (methodExecutionStatus(methodCheck) === 'blocked') {
          const error = (prepared && prepared.error) || '本地输入无法安全生成或提交';
          VCS.log('输入执行检查阻止提交:' + error, 'failc');
          showLisFailure('输入执行检查未通过', error, 'prepare');
          return;
        }
        if (needsRepair) {
          VCS.log('已生成智能修复预览；请选择“修复副本”或“保持原样”后继续', 'warnc');
          VCS.toast('请在方法检查中选择智能修复或保持原样');
          const method = $('lis-method-check');
          if (method && typeof method.scrollIntoView === 'function') {
            method.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
          return;
        }
        if (needsMethod) {
          VCS.log('旧版后端标记了方法待核对；不要求 ISPIN 确认，作业继续，自动 ΔE / 报告暂停', 'warnc');
        }
        if (!prepared || prepared.ok === false || prepared.error) {
          const error = (prepared && prepared.error) || '未知错误';
          if (!methodCheck && /方法.*(?:不一致|不兼容)|不能直接相减/.test(error)) {
            renderMethodCheck({
              execution_status: 'ready', comparability_status: 'incompatible',
              status: 'incompatible', issues: [error],
            });
          }
          VCS.log('Li-S 项目生成失败:' + error, 'failc');
          textList(prepared && prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
          showLisFailure('项目生成未完成', error, 'prepare');
          return;
        }
      }
      textList(prepared.advisories).forEach(x => VCS.log('方法学提示:' + x, 'warnc'));
      textList(prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
      if (['unverified', 'incompatible'].includes(methodComparabilityStatus(
        prepared && prepared.method_check))) {
        VCS.log('作业继续提交；自动 ΔE 与最终报告将等方法证据通过后再继续', 'warnc');
      }
      const projectPath = projectPathFrom(prepared);
      if (!projectPath) {
        VCS.log('Li-S 项目生成失败:后端没有返回项目路径', 'failc');
        showLisFailure('项目生成未完成', '后端没有返回项目路径', 'prepare');
        return;
      }
      if (!reusable) {
        State.preparedLis = {
          path: projectPath, fingerprint: operationFingerprint, name: val('pj-name'), submitted: false,
        };
        State.preparedConflictHint = null;
      }
      VCS.log('项目已生成，正在上传到 ' + val('lis-profile') + ' 并提交…', 'okc');
      if (button) button.textContent = '正在上传并提交整组…';
      const submitted = await submitLiSProject(projectPath, gate);
      if (!submitted) {
        VCS.log('已取消提交；本地项目仍保留在 ' + projectPath, 'warnc');
        showLisFailure('本地项目已生成，提交已取消',
          '没有提交任何新成员。再次点击主按钮即可直接重试，不会重复生成项目。', 'submit');
        return;
      }
      renderLisSubmitResult(prepared, submitted);
      submissionRows(submitted).forEach(row =>
        VCS.log(`${row.name}：${row.message || (row.ok ? '已提交' : '失败')}`, row.ok ? 'okc' : 'failc'));
      if (submitted.ok === false || submitted.error) {
        VCS.log('整组提交未完成:' + (submitted.error || '部分成员失败，请查看上方逐项结果'), 'failc');
        return;
      }
      if (State.preparedLis) State.preparedLis.submitted = true;
      State.workflowSubmitted = true;
      VCS.log('整组提交完成；自动续算、轻量下载和报告流程已开启', 'okc');
      if (window.Jobs && typeof window.Jobs.reload === 'function') await window.Jobs.reload();
      await reloadProjects();
      if (VCS.pipeline && typeof VCS.pipeline.reconfigure === 'function') await VCS.pipeline.reconfigure();
      VCS.toast('整组已提交；请保持软件运行以完成自动流程');
    } finally {
      State.lisBusy = false;
      syncLisInputLocks();
      if (button) updateLisReadiness();
    }
  }

  async function startLiS(referenceProject) {
    const ana = $('analysis-type');
    if (ana && ana.value !== 'adsorption') {
      ana.value = 'adsorption';
      ana.dispatchEvent(new Event('change', { bubbles: true }));
    }
    await reloadProjects(referenceProject);
    setAccordionOpen('pj-import-card', false);
    setAccordionOpen('pj-results-card', false);
    setAccordionOpen('pj-create-card', true);
    scrollToCard('pj-create-card');
    const sel = $('lis-reference');
    const hit = State.projects.find(p => p.path === referenceProject || p.name === referenceProject);
    if (sel && hit) { sel.value = hit.path; onReferenceChanged(); }
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
    else if (gate.methodBlocked) issue = '输入执行检查已阻止生成；请修正本目录输入错误';
    if (issue) { VCS.toast(issue, 'fail'); return; }
    const operationFingerprint = lisContentFingerprint();
    State.repairResume = 'create';
    State.lisBusy = true;
    syncLisInputLocks();
    if (btn) { btn.disabled = true; btn.textContent = '正在生成逐目录作业…'; }
    VCS.log(`仅生成：正在准备 clean slab + ${gate.items.length} 个逐成员四件套作业…`);
    try {
      const prepared = await VCS.call(
        'proj_prepare_lis', val('pj-name'), val('pj-slab'), gate.items,
        val('pj-incar'), val('pj-root'), gate.ref.path, gate.methodConfirmation,
        gate.memberIncars, gate.repairRequest);
      const methodCheck = prepared && prepared.method_check;
      const needsMethod = !!(prepared && prepared.needs_method_confirmation);
      const needsRepair = !!(prepared && prepared.needs_repair_decision);
      if (methodCheck || needsMethod) renderMethodCheck(methodCheck, needsMethod);
      if (methodExecutionStatus(methodCheck) === 'blocked') {
        const error = (prepared && prepared.error) || '本地输入无法安全生成';
        VCS.log('仅生成已停止：' + error, 'failc');
        showLisFailure('输入执行检查未通过', error, 'prepare');
        return;
      }
      if (needsRepair) {
        VCS.log('已生成智能修复预览；选择如何处理后再次执行“只生成”', 'warnc');
        VCS.toast('请先选择智能修复或保持原样');
        return;
      }
      if (needsMethod) VCS.log(
        '方法证据待核对；作业继续生成，自动 ΔE / 报告暂停', 'warnc');
      if (!prepared || prepared.ok === false || prepared.error) {
        const error = (prepared && prepared.error) || '未知错误';
        VCS.log('逐目录作业生成失败:' + error, 'failc');
        showLisFailure('项目生成未完成', error, 'prepare');
        return;
      }
      textList(prepared.advisories).forEach(x => VCS.log('方法学提示:' + x, 'warnc'));
      textList(prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
      const projectPath = projectPathFrom(prepared);
      if (!projectPath) {
        showLisFailure('项目生成未完成', '后端没有返回项目路径', 'prepare');
        return;
      }
      State.preparedLis = {
        path: projectPath, fingerprint: operationFingerprint,
        name: val('pj-name'), submitted: false,
      };
      State.preparedConflictHint = null;
      const resultBox = $('lis-submit-result');
      if (resultBox) {
        resultBox.hidden = false;
        resultBox.innerHTML = '<div class="lis-result-head ok"><b>逐目录作业已生成，尚未提交</b>' +
          `<span>${VCS.esc(projectPath)}</span></div>` +
          '<div class="lis-result-next"><b>输入已冻结</b><span>完整成员使用同目录原始四件套；不完整成员按本目录 POSCAR+INCAR 生成受管四件套。' +
          '可回到主按钮选择服务器并提交；不会重复生成项目。</span></div>';
      }
      VCS.log('逐目录作业已生成，尚未提交：' + projectPath, 'okc');
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      await reloadProjects();
      if (typeof VCS.nextStep === 'function') {
        VCS.nextStep({
          title: '逐目录吸附作业已生成',
          message: `clean slab 和 ${gate.items.length} 个吸附构型已按各自四件套证据加入任务列表。`,
          detail: '下一步可在当前页面选择服务器后点击主按钮提交；生成阶段不会覆盖任何成员的本地 INCAR。',
          primaryLabel: '留在当前页选择服务器',
        });
      }
    } finally {
      State.lisBusy = false;
      syncLisInputLocks();
      if (btn) { btn.disabled = false; btn.textContent = '只生成当前逐目录作业，不提交'; }
      updateLisReadiness();
    }
  }

  // ── 多项目选择:状态是唯一真相,DOM 仅负责显示 ─────────────────────────────
  function restoreCompareSelection() {
    if (State.compareSelectionRestored) return;
    State.compareSelectionRestored = true;
    try {
      const raw = JSON.parse(localStorage.getItem(COMPARE_PROJECTS_KEY) || '[]');
      if (Array.isArray(raw)) {
        State.comparePaths = new Set(raw.map(path => String(path || '')).filter(Boolean));
      }
    } catch (_) {
      State.comparePaths = new Set();
    }
  }

  function persistCompareSelection() {
    try {
      localStorage.setItem(COMPARE_PROJECTS_KEY, JSON.stringify(Array.from(State.comparePaths)));
    } catch (_) { /* 存储不可用不阻塞项目比较 */ }
  }

  function reconcileCompareSelection() {
    restoreCompareSelection();
    const known = new Set(State.projects.map(project => String(project.path || '')));
    let changed = false;
    Array.from(State.comparePaths).forEach(path => {
      if (!known.has(path)) {
        State.comparePaths.delete(path);
        changed = true;
      }
    });
    if (changed) persistCompareSelection();
  }

  function selectedComparePaths() {
    return State.projects
      .map(project => String(project.path || ''))
      .filter(path => path && State.comparePaths.has(path));
  }

  function setCompareSelection(paths) {
    const known = new Set(State.projects.map(project => String(project.path || '')));
    State.comparePaths = new Set((paths || [])
      .map(path => String(path || ''))
      .filter(path => path && known.has(path)));
    State.comparePreview = null;
    State.comparePreviewGeneration += 1;
    persistCompareSelection();
    renderFigProjList();
    scheduleComparePreview();
  }

  function comparePreviewProject(path) {
    const projects = State.comparePreview && State.comparePreview.projects;
    return Array.isArray(projects)
      ? projects.find(project => String(project.path || '') === String(path || ''))
      : null;
  }

  function comparisonCounts() {
    const selected = selectedComparePaths();
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
    const known = new Set(State.projects.map(project => String(project.path || '')));
    const valid = selected.filter(path => known.has(path)).length;
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
      box.innerHTML = '<span>已选 <b>' + counts.selected + '</b></span>' +
        '<span>有效 <b>' + counts.valid + '</b></span>' +
        '<span>阻断 <b>' + counts.blocked + '</b></span>' +
        '<span>可叠加台阶 <b>' + counts.overlay + '</b></span>';
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
      status.innerHTML = '<span class="pj-compare-empty">选择至少两个催化剂项目后，软件会先核对方法与反应路径。</span>';
      return;
    }
    if (!preview) {
      status.innerHTML = '<span class="pj-compare-empty">正在核对所选项目的数据、方法与台阶图口径…</span>';
      return;
    }
    if (preview.ok === false) {
      status.innerHTML = `<span class="pj-compare-error">${VCS.esc(preview.error || '比较预检失败')}</span>`;
      return;
    }
    let h = '';
    (preview.projects || []).forEach(project => {
      const ready = project.status === 'ready';
      const reasons = ready ? (project.warnings || []) : (project.block_reasons || []);
      h += `<div class="pj-compare-project ${ready ? 'ready' : 'blocked'}">` +
        `<b>${VCS.esc(project.display_name || project.name || pathBase(project.path))}</b>` +
        `<span>${ready ? '吸附能有效' : '已阻断'} · ${project.ladder ? '台阶可用' : '无可叠加台阶'}</span>` +
        (reasons.length ? `<small>${VCS.esc(reasons.join('；'))}</small>` : '') + '</div>';
    });
    const gate = preview.comparison_gate || {};
    (gate.blocking || []).forEach(reason => {
      h += `<div class="pj-compare-gate blocked">${VCS.esc(reason)}</div>`;
    });
    (gate.warnings || []).forEach(reason => {
      h += `<div class="pj-compare-gate warning">${VCS.esc(reason)}</div>`;
    });
    status.innerHTML = h || '<span class="pj-compare-empty">预检完成。</span>';
  }

  function legacyComparisonPreview(paths) {
    const projects = (paths || []).map(path => {
      const project = State.projects.find(item => String(item.path || '') === String(path || ''));
      const total = Number(project && project.n_members || 0);
      const done = Number(project && project.n_done || 0);
      const ready = !!project && total > 0 && done >= total;
      return {
        path,
        name: project && project.name || pathBase(path),
        display_name: project && project.name || pathBase(path),
        status: ready ? 'ready' : 'blocked',
        ladder: null,
        warnings: ready ? ['旧版后端未提供跨项目方法与路径预检'] : [],
        block_reasons: ready ? [] : [project ? '项目成员尚未全部完成' : '项目不存在或已移动'],
      };
    });
    const readyCount = projects.filter(project => project.status === 'ready').length;
    return {
      ok: true,
      legacy: true,
      selected_count: projects.length,
      ready_count: readyCount,
      ladder_ready_count: 0,
      projects,
      can_plot: false,
      comparison_gate: {
        status: 'unverified',
        blocking: [],
        warnings: ['当前后端未提供跨项目预检；生成图表或报告时仍会再次校验。'],
      },
    };
  }

  async function refreshComparePreview() {
    const paths = selectedComparePaths();
    const generation = ++State.comparePreviewGeneration;
    if (!paths.length) {
      State.comparePreview = null;
      renderCompareSummary();
      return;
    }
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const result = await VCS.call('proj_compare_preview', paths, preset || null);
    if (generation !== State.comparePreviewGeneration) return;
    if (bridgeMethodUnavailable(result)) {
      State.comparePreview = legacyComparisonPreview(paths);
    } else {
      State.comparePreview = result && result.ok !== false && !result.error
        ? result
        : { ok: false, error: (result && result.error) || '比较预检失败', projects: [] };
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
    setCompareSelection(State.projects.map(project => project.path));
  }

  async function selectComparableProjects() {
    const paths = State.projects.map(project => String(project.path || '')).filter(Boolean);
    if (!paths.length) return;
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const result = await VCS.call('proj_compare_preview', paths, preset || null);
    if (result && result.ok !== false && !result.error && Array.isArray(result.projects)) {
      setCompareSelection(result.projects
        .filter(project => project.status === 'ready')
        .map(project => project.path));
      return;
    }
    // 老后端没有预检接口时，只选已经完成全部成员的项目，不假装其台阶必然可比。
    setCompareSelection(State.projects.filter(project => {
      const total = Number(project.n_members || 0);
      return total > 0 && Number(project.n_done || 0) >= total;
    }).map(project => project.path));
    VCS.log('当前后端未提供跨项目预检，已暂按“成员全部完成”筛选；生成时仍会再次校验。', 'warnc');
  }

  // ── 已有项目:下拉 + 刷新 ──────────────────────────────────────────────────
  async function reloadProjects(preferredReference) {
    const sel = $('pj-select');
    const previous = sel ? sel.value : '';
    const [r, pipeline] = await Promise.all([
      VCS.call('proj_list'), VCS.call('pipeline_status'),
    ]);
    const listSucceeded = !!(r && r.ok !== false && !r.error && Array.isArray(r.projects));
    const pipelineByPath = new Map(((pipeline && pipeline.projects) || [])
      .map(item => [item.path, item]));
    const projectRows = listSucceeded ? r.projects : State.projects;
    State.projects = projectRows.map(project => {
      const progress = pipelineByPath.get(project.path) || {};
      return Object.assign({}, project, {
        pipeline_stage: progress.stage || '',
        pipeline_needs_human: !!progress.needs_human,
        pipeline_recover_round: Number(progress.recover_round || 0),
        n_done: progress.done != null ? Number(progress.done) : project.n_done,
        n_members: progress.total != null ? Number(progress.total) : project.n_members,
      });
    });
    State.comparePreview = null;
    State.comparePreviewGeneration += 1;
    if (listSucceeded) reconcileCompareSelection();
    else restoreCompareSelection();
    if (r && r.error) VCS.log('读取项目列表失败:' + r.error, 'failc');
    renderReferenceProjects(preferredReference);
    if (!sel) return;
    sel.innerHTML = '';
    if (!State.projects.length) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = '(暂无项目)';
      sel.appendChild(o);
      restoreWorkflowState(null);
      renderFigProjList();
      updateProjectSummary();
      updateJourney();
      refreshCandidateEvaluation('');
      scheduleComparePreview();
      return;
    }
    State.projects.forEach(p => {
      const o = document.createElement('option');
      o.value = p.path;
      const refs = referenceSpecies(p).length;
      o.textContent = (p.name || '(未命名)') + `（${p.n_members} 成员` +
        (refs ? ` · ${refs} 参考物种` : '') + '）';
      sel.appendChild(o);
    });
    let saved = '';
    try { saved = localStorage.getItem(CURRENT_PROJECT_KEY) || ''; } catch (_) { /* 不阻塞 */ }
    const active = State.projects.slice().reverse().find(p =>
      ['submit', 'monitor', 'recover'].includes(p.pipeline_stage) || p.pipeline_needs_human);
    const candidates = [preferredReference, previous, saved, active && active.path,
      State.projects[State.projects.length - 1].path];
    const want = candidates.find(path => State.projects.some(p => p.path === path)) || '';
    sel.value = want;
    try { localStorage.setItem(CURRENT_PROJECT_KEY, want); } catch (_) { /* 不阻塞 */ }
    restoreWorkflowState(State.projects.find(p => p.path === want));
    updateProjectSummary();
    updateJourney();
    renderFigProjList();
    refreshCandidateEvaluation(want);
    scheduleComparePreview();
  }

  // ── 论文级出图:多项目对比勾选列表(随项目列表刷新) ──────────────────────
  function renderFigProjList() {
    const box = $('fig-projlist');
    if (!box) return;
    box.innerHTML = '';
    if (!State.projects.length) {
      box.textContent = '(暂无项目)';
      return;
    }
    State.projects.forEach(p => {
      const lab = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.path = p.path;
      cb.checked = State.comparePaths.has(String(p.path || ''));
      const preview = comparePreviewProject(p.path);
      if (preview) lab.classList.add(preview.status === 'ready' ? 'ready' : 'blocked');
      cb.addEventListener('change', () => {
        if (cb.checked) State.comparePaths.add(String(p.path || ''));
        else State.comparePaths.delete(String(p.path || ''));
        State.comparePreview = null;
        State.comparePreviewGeneration += 1;
        persistCompareSelection();
        renderCompareSummary();
        scheduleComparePreview();
      });
      lab.appendChild(cb);
      const text = document.createElement('span');
      text.textContent = p.name || '(未命名)';
      lab.appendChild(text);
      if (preview) {
        const state = document.createElement('small');
        state.textContent = preview.status === 'ready'
          ? (preview.ladder ? '有效 · 台阶可用' : '有效 · 无台阶')
          : '阻断';
        lab.appendChild(state);
      }
      box.appendChild(lab);
    });
    renderCompareSummary();
  }

  function logFigResult(r, what) {
    if (!r || r.ok === false || r.error) {
      VCS.log(what + '失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    (r.files || []).forEach(f => VCS.log('已生成:' + f, 'okc'));
    (r.skipped || []).forEach(s => VCS.log(
      (s.partial ? '部分对比说明 ' : '跳过 ') + s.kind + ':' + s.reason, 'warnc'));
    (r.warnings || []).forEach(w => VCS.log('方法学提示:' + w, 'warnc'));
    if ((r.files || []).length) {
      VCS.log('图已输出到:' + r.out_dir, 'okc');
      VCS.call('open_dir', r.out_dir);          // 生成即可看
      VCS.toast('已生成 ' + r.files.length + ' 个文件');
    } else if ((r.skipped || []).length) {
      VCS.toast('本次没有可生成的图(原因见日志)', 'fail');
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
    if (!kinds.length) { VCS.log('请至少勾选一种图', 'failc'); return; }
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const btn = $('pj-figs');
    if (btn) btn.disabled = true;
    VCS.log('出图中(' + kinds.join('/') + (preset ? ',反应 ' + preset : '') + ')…');
    try {
      // preset 为空 → Li-S 默认(向后兼容);非空 → ladder 走通用反应引擎
      const r = await VCS.call('proj_figures', proj.path, kinds, null, preset || null);
      logFigResult(r, '出图');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // 多项目对比出图:勾选的项目 + 勾选的图类型调 proj_compare_figures
  async function makeCompareFigures() {
    const paths = selectedComparePaths();
    if (paths.length < 2) { VCS.log('多项目对比请勾选至少 2 个项目', 'failc'); return; }
    const kinds = [];
    if ($('fig-heatmap') && $('fig-heatmap').checked) kinds.push('heatmap');
    if ($('fig-scaling') && $('fig-scaling').checked) kinds.push('scaling');
    if ($('fig-volcano') && $('fig-volcano').checked) kinds.push('volcano');
    if ($('fig-compare-ladder') && $('fig-compare-ladder').checked) kinds.push('ladder');
    if (!kinds.length) { VCS.log('请至少勾选一种对比图', 'failc'); return; }
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    State.compareFiguresBusy = true;
    renderCompareSummary();
    VCS.log('对比出图中(' + paths.length + ' 个项目,' + kinds.join('/') + ')…');
    try {
      let r = await VCS.call('proj_compare_figures', paths, kinds, null, preset || null);
      const unsupported = r && r.error &&
        /(positional argument|unexpected argument|桥方法不存在|method not found)/i.test(String(r.error));
      if (unsupported) {
        const legacyKinds = kinds.filter(kind => kind !== 'ladder');
        r = legacyKinds.length
          ? await VCS.call('proj_compare_figures', paths, legacyKinds, null)
          : { ok: true, files: [], skipped: [] };
        if (!r) r = { ok: false, files: [], skipped: [], error: '旧版对比接口没有返回结果' };
        if (kinds.includes('ladder')) {
          r.skipped = (r.skipped || []).concat([{
            kind: 'ladder',
            reason: '当前后端版本尚不支持多项目台阶图，请升级后重试',
          }]);
        }
      }
      logFigResult(r, '对比出图');
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
    sel.innerHTML = '<option value="">Li-S 放电(默认)</option>';
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
    advance: '建议继续',
    hold_for_evidence: '先补证据',
    lower_priority: '降低优先级',
    blocked: '阻止判断',
  };
  const CLAIM_CEILING_ZH = {
    electronic_adsorption_screen: '电子吸附能初筛',
    corrected_thermodynamics: '热校正热力学',
    solvated_thermodynamics: '含溶剂化热力学',
    kinetically_supported: '动力学支持',
  };
  const SHORT_CHAIN_RISK_ZH = {
    high: '高：终产物过强结合警戒',
    medium: '中：需检查 Li₂S 分解/脱锂',
    weak_terminal_binding: '终产物结合偏弱',
    low: '低',
    unknown: '证据不足',
  };

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
      box.innerHTML = '<div class="pj-candidate-head"><b>候选评价暂不可用</b></div>' +
        `<p>${VCS.esc((result && result.error) || '后端没有返回可审计评价。')}</p>`;
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
    let html = `<div class="pj-candidate-head"><div><span>吸附能候选评价</span>` +
      `<b>${VCS.esc(CANDIDATE_PRIORITY_ZH[priority])}</b></div>` +
      '<small>Sabatier 初筛，不替代自由能与 NEB</small></div>' +
      `<p class="pj-candidate-summary">${VCS.esc(decision.summary_zh || '当前证据不足。')}</p>` +
      '<div class="pj-candidate-metrics">' +
      `<span><small>结论上限</small><b>${VCS.esc(CLAIM_CEILING_ZH[claim] || claim)}</b>` +
      `<code>${VCS.esc(claim)}</code></span>` +
      `<span><small>短链风险</small><b>${VCS.esc(SHORT_CHAIN_RISK_ZH[risk] || risk)}</b>` +
      `<code>${VCS.esc(risk)}</code></span></div>`;
    if (recommendations.length) {
      html += '<div class="pj-candidate-next"><b>建议的下一步（前 3 项）</b><ol>';
      recommendations.forEach(item => {
        const targets = (Array.isArray(item.targets) ? item.targets : []).map(String).join('、');
        html += '<li><b>' + VCS.esc(
          `${item.priority || 'P2'} · ${item.action_zh || item.code || '继续核验'}`) +
          '</b><span>' + VCS.esc(item.reason || '') +
          (targets ? `；目标：${VCS.esc(targets)}` : '') + '</span></li>';
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
      const reason = item.reason || '后端未明确报告此格式可用。';
      const labels = groups.get(reason) || [];
      labels.push(item.label);
      groups.set(reason, labels);
    });
    return Array.from(groups, ([reason, labels]) =>
      `${labels.join('、')}（${reason}）`).join('；');
  }

  function setReportCapabilityFailure(reason) {
    const detail = String(reason || '报告格式能力检测失败。').trim();
    const unavailableReason = `${detail}；未获得可用的明确确认。`;
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
          '后端未明确报告此格式可用。'),
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
        ? String(capability && capability.reason || '后端未明确报告此格式可用。') : '';
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
          if (description) description.textContent = disabledByCapability ? reason : item.description;
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
        ? '能力检测中' : '当前不可用';
      status.classList.toggle('bad', !valid);
      status.textContent = valid
        ? `已选择：${labels.split(' / ').join('、')}` +
          (unavailable.length ? `；${unavailablePrefix}：${unavailableText}` : '')
        : '请至少选择一种报告格式。' +
          (unavailable.length ? ` ${unavailablePrefix}：${unavailableText}` : '');
    }
    if (button) {
      const prefix = State.reportDiagnostic ? '生成诊断报告' : '生成报告';
      button.textContent = valid ? `${prefix}（${labels}）` : `${prefix}（请选择格式）`;
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
          result && result.error || '当前后端未提供报告格式能力检测。');
        return;
      }
      if (!result || result.ok === false) {
        setReportCapabilityFailure(result && result.error || '报告格式能力检测失败。');
        return;
      }
      if (!result.formats || typeof result.formats !== 'object') {
        setReportCapabilityFailure('能力检测未返回格式清单。');
        return;
      }
      applyReportCapabilities(result.formats);
    } catch (error) {
      if (generation !== State.reportCapabilityGeneration) return;
      setReportCapabilityFailure(
        error && error.message || '报告格式能力检测调用失败。');
    }
  }

  function requireReportFormats() {
    const formats = syncReportFormatControls();
    if (formats.length) return formats;
    VCS.log('生成报告前请至少选择一种格式（HTML、DOCX 或 PDF）。', 'failc');
    VCS.toast('请至少选择一种报告格式', 'fail');
    const first = $(SINGLE_REPORT_FORMATS[0].id);
    if (first && typeof first.focus === 'function') first.focus();
    return null;
  }

  async function refreshCandidateEvaluation(path) {
    const generation = ++State.candidateEvaluationGeneration;
    const wanted = String(path || '');
    if (!wanted) {
      hideCandidateEvaluation();
      return;
    }
    // 切换项目时先移除旧结论，避免慢请求把上一项目的分级暂时留在新项目名下。
    hideCandidateEvaluation();
    const result = await VCS.call('proj_evaluate_candidate', wanted);
    if (generation !== State.candidateEvaluationGeneration) return;
    if (!result || bridgeMethodUnavailable(result)) {
      hideCandidateEvaluation();
      return;
    }
    renderCandidateEvaluation(result);
  }

  function updateProjectSummary(deltaResult) {
    const box = $('pj-project-summary');
    const sel = $('pj-select');
    const path = sel ? sel.value : '';
    const project = State.projects.find(p => p.path === path);
    const deltaButton = $('pj-delta');
    const reportButton = $('pj-report');
    const csvButton = $('pj-csv');
    if (!box) return;
    if (!project) {
      box.innerHTML = '<b>还没有可查看的项目</b><span>下一步：先导入已算结果，或用参考能开始新的吸附计算。</span>';
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
      ? `逐物种参考 ${refs.length} 个：${refs.join('、')}`
      : ['species', 'species_refs'].includes(referenceMode) ? '逐物种参考（具体物种见 ΔE 表）'
        : ['single', 'gas_ref'].includes(referenceMode) ? '统一气相参考' : '参考模式待 ΔE 检查确认';
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
      stage = '方法不一致，ΔE 已阻断';
      next = '下一步：统一泛函、ENCUT、色散和 POTCAR 后重算。';
    } else if (complete) {
      stage = 'ΔE 已完整'; next = '下一步：后台会自动生成 HTML、Word 与 PDF，也可立即手动生成。';
    } else if (rows && rows.length && rows.every(row => row.delta_e != null) && backendFinal === false) {
      stage = 'ΔE 可预览，最终报告仍被门禁阻止';
      next = `下一步：${deltaResult.final_report_reason || '补齐参考态与方法确认。'}`;
    } else if (missing != null && missing > 0) {
      stage = `${missing} 个构型尚缺可靠 ΔE`;
      next = '下一步：保持软件运行等待自动下载/续算，完成后点“刷新并计算 ΔE”。';
    } else if (done != null && total && done < total) {
      stage = `自动运行中 ${done}/${total}`;
      next = '下一步：保持软件运行；任务完成后回来刷新 ΔE。';
    } else if (!total && refs.length) {
      stage = '参考能库已就绪'; next = '下一步：用这些参考能开始新的 slab / adsorption 计算。';
    } else if (pipelineStage === 'submit') {
      stage = '作业已生成，等待提交';
      next = '下一步：到任务页提交尚未上传的成员；此时自动监控尚未开始。';
    } else if (pipelineStage) {
      stage = `管线阶段：${pipelineStage}`;
      next = '下一步：保持软件运行；任务完成后点“刷新并计算 ΔE”。';
    } else {
      stage = State.workflowSubmitted ? '自动托管运行中' : '等待结果检查';
      next = '下一步：点击“刷新并计算 ΔE”，软件会明确列出仍缺少的结果。';
    }
    box.innerHTML = `<div class="pj-summary-chips"><span>${VCS.esc(total ? `成员 ${done == null ? '?' : done}/${total}` : '参考项目')}</span>` +
      `<span>${VCS.esc(refText)}</span><span>${VCS.esc(stage)}</span></div><b>${VCS.esc(next)}</b>`;
    if (deltaButton) deltaButton.classList.toggle('primary', !complete);
    if (reportButton) reportButton.classList.toggle('primary', complete);
    State.reportDiagnostic = !complete;
    syncReportFormatControls();
  }

  function currentProject() {
    const sel = $('pj-select');
    const path = sel ? sel.value : '';
    if (!path) { VCS.log('请先选择一个项目', 'failc'); return null; }
    return State.projects.find(p => p.path === path) || { path, name: '' };
  }

  // ── 计算 ΔE:proj_delta → 表格渲染(缺员门控原样展示,绝不编数) ──────────
  async function delta() {
    const proj = currentProject();
    if (!proj) return null;
    const box = $('pj-table');
    if (box) box.innerHTML = '';
    VCS.log('计算项目「' + (proj.name || '') + '」的 ΔE…');
    const r = await VCS.call('proj_delta', proj.path);
    if (!r || r.ok === false || r.error) {
      VCS.log('计算 ΔE 失败:' + ((r && r.error) || '未知错误'), 'failc');
      updateProjectSummary();
      return null;
    }
    renderDelta(r);
    refreshCandidateEvaluation(proj.path);
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
      VCS.toast('ΔE 已完整；下一步生成完整报告');
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
      return '下一步：按方法检查列出的泛函、ENCUT、色散、共享元素 POTCAR/DFT+U 等硬冲突逐项修正后重算。合法的 ISPIN 差异本身不会触发此阻断。';
    }
    if (methodCheck.status === 'unverified') {
      return '下一步：按方法检查逐项核对；分子参考、clean slab 与吸附构型可采用各自正确的基态自旋，自旋差异本身只作提示。';
    }
    if (/构型未完成|清洁表面未完成|未完成/.test(note)) {
      return '下一步：保持软件运行等待自动续算/下载；任务 DONE 后重新计算 ΔE。';
    }
    if (/无匹配物种参考|参考能量/.test(note)) {
      return '下一步：确认该构型的物种映射，并导入同名 Li-S 参考能。';
    }
    if (/能量缺失|不合理/.test(note)) {
      return '下一步：拉回 OUTCAR / OSZICAR，若仍无可靠 E0 则修复或续算该成员。';
    }
    return '下一步：查看备注中的缺项，修正后点“刷新并计算 ΔE”。';
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
      h += '<div class="pj-method-gate incompatible"><b>方法不一致：ΔE 已阻断</b>' +
        '<span>请按下列泛函、ENCUT、色散、共享元素 POTCAR/DFT+U 等硬冲突逐项修正后重新计算。合法的 ISPIN 差异不属于硬冲突。</span>' +
        (methodIssues.length ? `<ul>${methodIssues.map(x => `<li>${VCS.esc(x)}</li>`).join('')}</ul>` : '') +
        '</div>';
    } else if (methodStatus === 'unverified') {
      h += '<div class="pj-method-gate unverified"><b>方法一致性尚未完全核验</b>' +
        '<span>请按下列具体项目逐项核对；分子参考、clean slab 与吸附构型可采用各自正确的基态自旋，自旋差异本身只作提示。</span>' +
        (methodWarnings.length ? `<ul>${methodWarnings.map(x => `<li>${VCS.esc(x)}</li>`).join('')}</ul>` : '') +
        '</div>';
    } else if (methodStatus === 'verified') {
      h += '<div class="pj-method-gate verified"><b>方法一致性已核验</b>' +
        '<span>本项目能量相减项已通过记录层面的一致性检查。</span></div>';
    }
    if (methodAdvisories.length) {
      h += '<div class="pj-method-gate advisory"><b>体系自旋提示（不阻断 ΔE）</b>' +
        '<span>分子参考与周期体系 ISPIN 不同可以是合理的基态设置；分子、clean slab 与吸附体系可分别采用各自经验证的基态自旋；以下内容仅用于审计与复核。</span>' +
        `<ul>${methodAdvisories.map(x => `<li>${VCS.esc(x)}</li>`).join('')}</ul></div>`;
    }
    if (!rows.length) {
      h += '<div class="empty"><p>该项目暂无吸附构型成员</p></div>';
      box.innerHTML = h;
      return;
    }
    const referenceMode = String(r.reference_mode || '').toLowerCase();
    h += '<table><thead><tr>' +
      '<th>构型</th><th>状态</th><th class="num">E_config (eV)</th>' +
      '<th>参考物种 / E_ref (eV)</th><th class="num">ΔE (eV)</th>' +
      '<th class="num">ΔΔE (eV)</th><th>组内比较</th><th>公式与备注</th>' +
      '</tr></thead><tbody>';
    rows.forEach(row => {
      const rowMode = String(row.reference_mode || referenceMode || '').toLowerCase();
      const species = row.reference_species || row.species || row.ref_species || '';
      const eRef = row.e_ref != null ? row.e_ref
        : row.reference_energy_e0_eV != null ? row.reference_energy_e0_eV : row.e_reference;
      let refLabel;
      if (['species', 'species_refs'].includes(rowMode) || species) {
        refLabel = `${species || '逐物种参考'} / ${fmt(eRef, 6)}`;
      } else if (['single', 'gas_ref'].includes(rowMode) || eRef != null) {
        refLabel = `统一气相参考 / ${fmt(eRef, 6)}`;
      } else {
        refLabel = '参考信息待确认';
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
        `<td>${row.delta_e == null ? '—' : row.is_most_stable ? '<span class="pj-stable">最稳构型</span>' : '同物种对照'}</td>` +
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
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;   // 用户取消
    const save = joinPath(dr.path, (proj.name || 'project') + '_delta_e.csv');
    VCS.log('导出 ΔE 表到:' + save + '…');
    const r = await VCS.call('proj_export_csv', proj.path, save);
    if (!r || r.ok === false || r.error) {
      VCS.log('导出 CSV 失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    VCS.log('已导出 CSV:' + (r.file || save), 'okc');
    VCS.call('open_dir', r.file || save);       // 输出反馈统一:打开所在目录
    VCS.toast('已导出 CSV');
  }

  function bridgeMethodUnavailable(result) {
    const message = String(result && result.error || '');
    return /(桥方法不存在|method not found|unknown method|has no attribute)/i.test(message);
  }

  function reportScienceState(result) {
    const raw = String(result && (
      result.scientific_status || result.report_status || result.report_kind || result.kind
    ) || 'pending').trim().toLowerCase();
    if (raw === 'final') return { key: 'final', label: '最终' };
    if (raw === 'diagnostic') return { key: 'diagnostic', label: '诊断' };
    if (raw === 'draft') return { key: 'draft', label: '草稿' };
    if (raw === 'blocked') return { key: 'blocked', label: '阻断' };
    return { key: 'pending', label: '待判定' };
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
      ? `已生成 ${files.length} 个文件但未登记`
      : artifactRaw === 'stale'
        ? '产物已过期'
        : generated
          ? `已生成 ${files.length} 个文件`
          : (productKey === 'failed' ? '生成失败' : '尚未生成');
    const reason = String(result && (result.gate_reason || result.report_reason) || '');
    const gateRaw = String(result && result.publication_gate_status || 'unknown').toLowerCase();
    const gate = gateRaw === 'eligible'
      ? { key: 'eligible', label: '可发布最终版' }
      : gateRaw === 'blocked'
        ? { key: 'blocked', label: '阻断' }
        : gateRaw === 'pending'
          ? { key: 'pending', label: '等待计算' }
          : { key: 'pending', label: '未知' };
    return '<div class="pj-report-states" role="status" aria-label="报告产物状态、科学状态与发布门禁">' +
      `<span class="pj-report-state product ${productKey}"><b>报告产物</b>${VCS.esc(productLabel)}</span>` +
      `<span class="pj-report-state science ${science.key}"${reason ? ` title="${VCS.esc(reason)}"` : ''}>` +
      `<b>科学状态</b>${VCS.esc(science.label)}</span>` +
      `<span class="pj-report-state gate ${gate.key}"${reason ? ` title="${VCS.esc(reason)}"` : ''}>` +
      `<b>发布门禁</b>${VCS.esc(gate.label)}</span></div>`;
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
        if (key === 'comparison') next = [...trail, '批次比较'];
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
    let html = '<div class="pj-report-head"><b>' + VCS.esc(heading || '报告文件') + '</b>' +
      `<span>${files.length} 个文件</span></div>${stateMarkup}` +
      '<div class="pj-report-links">';
    files.forEach(file => {
      const name = pathBase(file.path);
      html += `<button type="button" class="btn quiet pj-report-file" data-report-open="${VCS.esc(file.path)}">` +
        `<b>${VCS.esc(file.label || name)}</b><small>${VCS.esc(name)}</small></button>`;
    });
    html += '</div>';
    if (diagnostic) {
      html += '<div class="pj-report-note">当前生成的是诊断报告：保留真实结果与阻断原因，不冒充最终结论。</div>';
    }
    const gateReason = String(result && (result.gate_reason || result.report_reason) || '');
    if (gateReason) {
      html += `<div class="pj-report-note">发布门禁：${VCS.esc(gateReason)}</div>`;
    }
    box.innerHTML = html;
    box.querySelectorAll('[data-report-open]').forEach(button => {
      button.addEventListener('click', () => VCS.call('open_dir', button.dataset.reportOpen));
    });
  }

  async function legacyBatchReports(paths, outDir) {
    const individual = [];
    const failures = [];
    for (const [index, path] of paths.entries()) {
      const project = State.projects.find(item => String(item.path || '') === String(path || ''));
      const name = String(project && project.name || `project_${index + 1}`);
      const safeName = name.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').trim() || `project_${index + 1}`;
      const save = joinPath(outDir, `${String(index + 1).padStart(2, '0')}_${safeName}_完整报告.html`);
      const result = await VCS.call('proj_report', path, save, true);
      if (result && result.ok !== false && !result.error) {
        const returnedFiles = result.files && typeof result.files === 'object' &&
          !Array.isArray(result.files) ? result.files : {};
        individual.push(Object.assign({}, result, {
          name,
          files: Object.assign({}, returnedFiles, { html: result.file || returnedFiles.html || save }),
          ok: true,
        }));
      } else {
        failures.push(`${name}: ${(result && result.error) || '未知错误'}`);
      }
    }
    return {
      ok: individual.length > 0,
      kind: 'diagnostic',
      files: { individual },
      warnings: [
        '当前后端仅支持逐项目 HTML；没有生成跨项目比较报告或 Word/PDF。',
        ...failures,
      ],
      out_dir: outDir,
      error: individual.length ? null : failures.join('；') || '旧版批次报告生成失败',
    };
  }

  // ── 单项目报告:同一快照生成 HTML + Word + PDF；旧后端回退 HTML ───────────
  async function report() {
    const proj = currentProject();
    if (!proj) return;
    const selectedFormats = requireReportFormats();
    if (!selectedFormats) return;
    const selectedLabel = reportFormatLabel(selectedFormats);
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;   // 用户取消
    State.reportBusy = true;
    syncReportFormatControls();
    const output = $('pj-report-files');
    if (output) output.innerHTML = '';
    VCS.log(`正在从同一份数据快照生成 ${selectedLabel} 报告，可能需要几分钟…`);
    try {
      let r = await VCS.call(
        'proj_report_bundle', proj.path, dr.path, selectedFormats, true);
      if (bridgeMethodUnavailable(r)) {
        if (selectedFormats.length !== 1 || selectedFormats[0] !== 'html') {
          r = {
            ok: false,
            artifact_status: 'failed',
            kind: null,
            scientific_status: null,
            error: '当前后端仅支持 HTML；请只勾选 HTML 后重试，或升级后端以导出 DOCX/PDF。',
          };
          VCS.log(r.error, 'failc');
          renderReportFiles('pj-report-files', r, '单项目报告');
          return;
        }
        const save = joinPath(dr.path, (proj.name || 'project') + '_完整报告.html');
        const legacy = await VCS.call('proj_report', proj.path, save, true);
        r = legacy && !legacy.error
          ? Object.assign({}, legacy, { files: { html: legacy.file || save } })
          : legacy;
        VCS.log('当前后端仅支持 HTML，已使用兼容模式生成。', 'warnc');
      }
      if (!r || r.ok === false || r.error) {
        VCS.log('生成完整报告失败:' + ((r && r.error) || '未知错误'), 'failc');
        renderReportFiles('pj-report-files', r, '单项目报告');
        return;
      }
      collectReportFiles(r.files || { html: r.file }).forEach(file => {
        VCS.log('报告已生成:' + file.path, 'okc');
      });
      (r.warnings || []).forEach(warning => VCS.log('报告提示:' + warning, 'warnc'));
      if (r.kind === 'diagnostic') {
        VCS.log('最终报告门禁未通过，已生成带阻断原因的诊断报告。', 'warnc');
      }
      renderReportFiles('pj-report-files', r, '单项目报告');
      VCS.call('open_dir', r.out_dir || dr.path);
      // 重新读取管线状态；只有后端已经落下与当前输入/结果哈希绑定的
      // report_done 标记时，界面才把整个自动流程显示为完成。
      await reloadProjects(proj.path);
      VCS.toast(r.kind === 'diagnostic' ? `${selectedLabel} 诊断报告已生成` : `${selectedLabel} 报告已生成`);
    } finally {
      State.reportBusy = false;
      syncReportFormatControls();
    }
  }

  async function batchReport() {
    const paths = selectedComparePaths();
    if (paths.length < 2) {
      VCS.log('批次报告请至少选择 2 个催化剂项目', 'failc');
      return;
    }
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;
    const preset = $('pj-preset') ? $('pj-preset').value : '';
    const output = $('pj-batch-files');
    State.batchReportBusy = true;
    renderCompareSummary();
    if (output) output.innerHTML = '';
    VCS.log(`正在生成 ${paths.length} 个单项目报告与一份批次比较报告…`);
    try {
      let r = await VCS.call(
        'proj_batch_report', paths, dr.path, preset || null,
        ['html', 'docx', 'pdf'], true, true);
      if (bridgeMethodUnavailable(r)) {
        r = await legacyBatchReports(paths, dr.path);
      }
      if (!r || r.ok === false || r.error) {
        VCS.log('批次报告生成失败:' + ((r && r.error) || '未知错误'), 'failc');
        renderReportFiles('pj-batch-files', r, '批次报告');
        return;
      }
      collectReportFiles(r.files).forEach(file => VCS.log('报告已生成:' + file.path, 'okc'));
      (r.blocked || []).forEach(reason => VCS.log('比较阻断:' + reason, 'warnc'));
      (r.warnings || []).forEach(reason => VCS.log('比较提示:' + reason, 'warnc'));
      renderReportFiles('pj-batch-files', r, '多催化剂批次报告');
      VCS.call('open_dir', r.out_dir || dr.path);
      VCS.toast(r.kind === 'diagnostic'
        ? '批次诊断报告已生成' : '批次 HTML / Word / PDF 报告已生成');
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
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;
    const btn = $('pj-draft');
    const box = $('pj-draft-out');
    if (btn) btn.disabled = true;
    if (box) box.innerHTML = '';
    VCS.log('生成成稿包(SI + 三线表 + 口径稽核 + 方法学),生成中…');
    try {
      const r = await VCS.call('draft_ready', proj.path, dr.path);
      if (!r || r.ok === false && r.error) {
        // ok=False 但有产物(稽核不过仍出全套)时 error 为 null;仅真错误(error 非空)才失败
        if (r && r.error) { VCS.log('成稿包生成失败:' + r.error, 'failc'); return; }
      }
      if (!r) { VCS.log('成稿包生成失败:未知错误', 'failc'); return; }
      VCS.log((r.ok ? '✓ 稽核通过:' : '⚠ 稽核未通过(仍产全套工件):') + (r.summary || ''),
        r.ok ? 'okc' : 'warnc');
      (r.products || []).forEach(p => VCS.log('产物:' + p, 'okc'));
      (r.issues || []).forEach(i => VCS.log(i, 'warnc'));
      renderDraft(r);
      if (r.out_dir) VCS.call('open_dir', r.out_dir);
      VCS.toast(r.ok ? '成稿包已生成' : '成稿包已生成(有待确认项)', r.ok ? '' : 'fail');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderDraft(r) {
    const box = $('pj-draft-out');
    if (!box) return;
    let h = `<div class="pj-note" style="color:var(--${r.ok ? 'ok' : 'warn'})">` +
      VCS.esc(r.summary || '') + `(待确认 ${r.issues_total || 0} 项)</div>`;
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
    ['pj-import-name', 'pj-import-root'].forEach(id => {
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
    wire('pj-report', report);
    SINGLE_REPORT_FORMATS.forEach(item => {
      const input = $(item.id);
      if (input) input.addEventListener('change', syncReportFormatControls);
    });
    syncReportFormatControls();
    loadReportCapabilities();
    const projectSelect = $('pj-select');
    if (projectSelect) projectSelect.addEventListener('change', () => {
      restoreWorkflowState(State.projects.find(p => p.path === projectSelect.value));
      try { localStorage.setItem(CURRENT_PROJECT_KEY, projectSelect.value); } catch (_) { /* 不阻塞 */ }
      updateProjectSummary(); updateJourney();
      refreshCandidateEvaluation(projectSelect.value);
    });
    wire('pj-figs', makeFigures);
    wire('pj-cmpfigs', makeCompareFigures);
    wire('fig-select-all', selectAllCompareProjects);
    wire('fig-select-comparable', selectComparableProjects);
    wire('fig-select-clear', () => setCompareSelection([]));
    wire('pj-batch-report', batchReport);
    const presetSelect = $('pj-preset');
    if (presetSelect) presetSelect.addEventListener('change', () => {
      State.comparePreview = null;
      State.comparePreviewGeneration += 1;
      renderFigProjList();
      scheduleComparePreview();
    });
    wire('pj-draft', draftReady);
    const analysis = $('analysis-type');
    if (analysis) analysis.addEventListener('change', () => {
      if (analysis.value === 'spin') {
        analysis.value = 'taskana';
        analysis.dispatchEvent(new Event('change', { bubbles: true }));
        VCS.navigate('jobs', { source: 'spin-analysis' });
        VCS.toast('自旋态对比在任务页：勾选同一家族后点击“自旋对比”');
      }
    });
    renderConfigs();
    updateJourney();
    loadPresets();
    reloadProjects();
    loadLisProfiles();
  }

  // 导入向导/任务页优先用 project.yaml 绝对路径精确选中，
  // 避免两个同名项目被选错。按名选中仅保留给旧数据兼容。
  async function selectByPath(path) {
    await reloadProjects();
    const wanted = String(path || '');
    const hit = State.projects.find(p => String(p.path || '') === wanted);
    const sel = $('pj-select');
    if (hit && sel) {
      sel.value = hit.path;
      restoreWorkflowState(hit);
      updateProjectSummary();
      updateJourney();
      refreshCandidateEvaluation(hit.path);
    }
    return !!hit;
  }

  async function selectByName(name) {
    await reloadProjects();
    const hit = State.projects.find(p => p.name === name);
    const sel = $('pj-select');
    if (hit && sel) {
      sel.value = hit.path;
      restoreWorkflowState(hit);
      updateProjectSummary();
      updateJourney();
      refreshCandidateEvaluation(hit.path);
    }
  }

  // 切回项目页时刷新项目下拉
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'project') { reloadProjects(); loadLisProfiles(); }
  });
  document.addEventListener('vcs:scenario', e => applyProjectMode(e.detail && e.detail.scenario));
  document.addEventListener('vcs:calculation', applyProjectCalculation);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Project = { reload: reloadProjects, selectByPath, selectByName, openImport, startLiS };
})();
