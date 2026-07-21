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

  const State = {
    configs: [],      // 构型 POSCAR 路径列表(逐个添加)
    configSpecies: Object.create(null), // path -> Li-S 物种；旧 proj_create 仍只读取 configs 字符串数组
    projects: [],     // proj_list 返回:[{path,name,n_members}]
    profiles: [],     // list_profiles 返回；一站式提交资源选择
    importRows: [],   // 本地结果扫描候选(前端只持有修正值;commit 时后端会重新验证)
    importResult: null,
    preparedLis: null, // {path,fingerprint,name,submitted}:生成成功后复用，安全重试提交
    preparedConflictHint: null,
    methodCheck: null,
    methodCheckFingerprint: '',
    needsMethodConfirmation: false,
    workflowSubmitted: false,
    workflowResultReady: false,
    workflowStage: '',
    workflowNeedsHuman: false,
    workflowProjectPath: '',
    lisBusy: false,
  };

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
    State.workflowSubmitted = ['submit', 'monitor', 'recover'].includes(stage);
    State.workflowResultReady = stage === 'report_done';
  }

  function updateJourney() {
    const status = $('ads-journey-status');
    const refs = State.projects.filter(p => referenceSpecies(p).length > 0);
    const gate = lisGate();
    let current = 1;
    if (State.workflowResultReady || ['analysis', 'report_done'].includes(State.workflowStage)) current = 6;
    else if (State.workflowSubmitted) current = 5;
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
      if (status) status.textContent = '任务和报告已完成，可以查看 ΔE 与最新报告。';
      const resultButton = $('ads-route-results');
      if (resultButton) resultButton.textContent = '查看 ΔE 与报告';
      setJourneyPrimary('ads-route-results');
    } else if (State.workflowStage === 'analysis') {
      if (status) status.textContent = '整组任务已完成。下一步检查 ΔE；全部有效后即可生成报告。';
      const resultButton = $('ads-route-results');
      if (resultButton) resultButton.textContent = '检查 ΔE 并生成报告';
      setJourneyPrimary('ads-route-results');
    } else if (State.workflowSubmitted) {
      if (status) status.textContent = '整组任务已提交，自动托管正在监控、续算和下载关键结果。请保持软件运行。';
      const newButton = $('ads-route-new');
      if (newButton) newButton.textContent = '查看任务进度';
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
    if (sel && hit) sel.value = hit.path;
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
    })[action] || action;
  }

  function importStatus(row) {
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
    const out = { total: State.importRows.length, ready: 0, review: 0, blocked: 0 };
    State.importRows.forEach(row => { out[importStatus(row)] += 1; });
    return out;
  }

  function importMethodGate() {
    const selected = selectedImportRows();
    const unresolved = selected.filter(row => importStatus(row) !== 'ready');
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
      String(row.state).toUpperCase() !== 'DONE');
    const invalidTasks = selected.filter(row => !['relax', 'static', 'freq', 'dos', 'band'].includes(row.taskType));
    const missingConfirmationReasons = selected.filter(row => row.manualConfirm &&
      row.confirmationEligible && !row.confirmationReason.trim());
    let issue = '';
    if (!selected.length) issue = '请至少勾选一个可导入结果';
    else if (missingConfirmationReasons.length) issue =
      `有 ${missingConfirmationReasons.length} 个人工确认结果尚未填写核对依据`;
    else if (unresolved.length) issue = `仍有 ${unresolved.length} 个已选结果需要处理`;
    else if (clean > 1) issue = '一个吸附能项目只能选择 1 个清洁表面';
    else if (gas > 1) issue = '一个吸附能项目只能选择 1 个吸附质气相参考';
    else if (missingSpecies.length) issue = `有 ${missingSpecies.length} 个分子参考未填写物种名称（如 Li2S4、S8）`;
    else if (missingConfigSpecies.length) issue =
      `已选择逐物种分子参考：请为 ${missingConfigSpecies.length} 个吸附构型填写对应物种（如 Li2S8）`;
    else if (unsafeMolecules.length) issue = '分子能量库只接收自动判定 DONE 的结果；待核项请改为“独立计算结果”';
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
      ['review', n.review, '需你确认'],
      ['blocked', n.blocked, '暂不可导入'],
    ].map(x => `<div class="pj-import-stat ${x[0]}"><b>${x[1]}</b><span>${x[2]}</span></div>`).join('');
    const gate = importMethodGate();
    const pending = n.review + n.blocked;
    attention.classList.toggle('warn', pending > 0 || !gate.ok);
    if (pending) {
      attention.textContent = `软件已先选中 ${n.ready} 个可靠结果。请处理黄色/红色条目；点击每行“查看证据”可看到无法导入的具体原因。`;
    } else {
      attention.textContent = '所有结果均已通过检查。确认清洁表面和吸附构型角色后即可建立项目。';
    }
  }

  function rowMatches(row) {
    const filter = val('pj-import-filter') || 'attention';
    const query = val('pj-import-search').toLowerCase();
    const status = importStatus(row);
    if (filter === 'attention' && status === 'ready') return false;
    if (filter !== 'all' && filter !== 'attention' && filter !== status) return false;
    if (!query) return true;
    const haystack = [row.name, row.path, row.role, row.taskType, row.state, row.action,
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
    body.innerHTML = visible.map(row => {
      const status = importStatus(row);
      const statusLabel = { ready: '可导入', review: '需确认', blocked: '暂不可导入' }[status];
      const statusClass = { ready: 'ok', review: 'warn', blocked: 'fail' }[status];
      const reasons = [...row.blocking, ...row.diagnosis, ...row.warnings, ...row.evidence];
      const mainReason = row.blocking[0] || row.diagnosis[0] || row.warnings[0] ||
        (status === 'ready' ? '能量与收敛证据已通过检查' : '尚缺少足够的完成证据');
      const detail = reasons.length ? '<details><summary>查看证据与完整原因</summary><ul class="pj-import-evidence">' +
        reasons.map(item => `<li>${VCS.esc(item)}</li>`).join('') + '</ul></details>' : '';
      const roleOptions = ['clean_slab', 'config', 'gas_ref', 'molecule_ref', 'standalone', 'ignore'];
      const taskOptions = ['unknown', 'relax', 'static', 'freq', 'dos', 'band'];
      const disabled = status === 'blocked' && !row.confirmationEligible ? ' disabled' : '';
      const speciesInput = ['config', 'molecule_ref'].includes(row.role)
        ? `<input class="ipt pj-import-species" data-act="species" value="${VCS.esc(row.species)}" ` +
          `placeholder="${row.role === 'molecule_ref' ? '物种，如 Li2S4' : '吸附物种，如 Li2S8（使用分子参考时必填）'}">`
        : '';
      const confirmationInput = row.confirmationEligible
        ? `<textarea class="ipt pj-import-confirm-reason" data-act="confirm-reason" rows="2" ` +
          `placeholder="请填写你核对了哪些输出证据">${VCS.esc(row.confirmationReason)}</textarea>`
        : '';
      const manualLabel = status === 'ready' ? '自动检查已通过，无需人工确认'
        : row.confirmationEligible ? '我已核对并确认收敛' : '存在硬性问题，不能人工跳过';
      const manualHint = status === 'ready' ? '可直接导入；提交时仍会复核源文件是否变化'
        : row.confirmationEligible ? '请保留可审计的核对依据；提交时仍会复核硬性门禁'
          : '请按左侧原因补齐结果';
      return `<tr data-import-index="${row.index}" class="pj-import-${status}">` +
        `<td class="pj-import-check"><input type="checkbox" data-act="select"${row.selected ? ' checked' : ''}${disabled}></td>` +
        `<td class="pj-import-dir"><span class="name" title="${VCS.esc(row.path)}">${VCS.esc(row.name)}</span>` +
        `<span class="sub" title="${VCS.esc(row.path)}">${VCS.esc(row.path)}</span></td>` +
        `<td><select class="ipt" data-act="role">${optionHtml(roleOptions, row.role, ROLE_LABELS)}</select>${speciesInput}</td>` +
        `<td><select class="ipt" data-act="task">${optionHtml(taskOptions, row.taskType, TASK_LABELS)}</select></td>` +
        `<td class="pj-import-reason"><span class="pill ${statusClass}"><i></i>${statusLabel}</span> ` +
        `<span class="pj-import-mainreason">${VCS.esc(mainReason)}</span>` +
        (row.action ? `<span class="pj-import-action">下一步：${VCS.esc(row.action)}</span>` : '') + detail + '</td>' +
        `<td class="pj-import-manual"><label><input type="checkbox" data-act="confirm"` +
        `${row.manualConfirm ? ' checked' : ''}${row.confirmationEligible ? '' : ' disabled'}>` +
        `${manualLabel}</label><span class="sub">${manualHint}</span>` +
        `${confirmationInput}</td></tr>`;
    }).join('');
    empty.hidden = visible.length > 0;
    body.querySelectorAll('tr[data-import-index]').forEach(tr => {
      const row = State.importRows[Number(tr.dataset.importIndex)];
      tr.querySelector('[data-act="select"]').addEventListener('change', e => {
        row.selected = e.target.checked;
        updateImportCommit();
      });
      tr.querySelector('[data-act="role"]').addEventListener('change', e => {
        row.role = e.target.value;
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
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      await reloadProjects();
      const sel = $('pj-select');
      const hit = State.projects.find(p => p.path === r.project_path || p.name === (r.project_name || name));
      if (sel && hit) sel.value = hit.path;
      // 建好项目后立即刷新 ΔE 表，让缺角色/缺能量在同一页直接可见；报告仍由用户
      // 明确触发，避免自动产出一个科学数据尚不完整的文件。
      const deltaResult = await delta();
      const deltaRows = (deltaResult && deltaResult.rows) || [];
      const isAdsorption = gate.clean > 0 && gate.configs > 0;
      const hasIncompleteDelta = isAdsorption &&
        (!deltaRows.length || deltaRows.some(row => row.delta_e == null));
      const hasNeedsHuman = Number(r.summary && r.summary.needs_human || 0) > 0;
      // A molecule-reference-only import is a reusable reference library, not an
      // adsorption project with a reportable data point.  Keep the report action
      // locked until both clean slab and at least one config are present.
      const reportReady = isAdsorption && !hasNeedsHuman && !hasIncompleteDelta;
      const referenceOnly = gate.molecules > 0 && !isAdsorption;
      const startWithReferences = () => startLiS(
        r.project_path || r.path || r.project_name || name);
      showImportDone(r, gate.selected.length, reportReady, gate.molecules > 0, referenceOnly);
      setImportStep(4);
      VCS.toast(referenceOnly ? 'Li-S 参考能库已建立，可以开始新的吸附计算'
        : reportReady ? '结果与 ΔE 已载入，可以生成报告' : '结果已导入；请先处理表格中的缺项');
      if (typeof VCS.nextStep === 'function') {
        VCS.nextStep({
          title: '结果导入完成',
          message: `已导入 ${gate.selected.length} 个结果并建立项目「${r.project_name || name}」。`,
          detail: referenceOnly
            ? '这些已收敛的 Li-S 能量已加入参考库。下一步只需选择固定 INCAR、clean slab 和 adsorption 结构。'
            : reportReady
            ? 'ΔE 已自动计算并显示在本页。请核对表格后生成完整报告。'
            : 'ΔE 表已自动刷新，但仍有缺角色、缺能量或待确认结果；处理完再生成报告。',
          primaryLabel: referenceOnly ? '用这些参考能开始吸附计算'
            : reportReady ? '生成完整报告' : '查看 ΔE 缺项',
          stayLabel: referenceOnly ? '先检查参考能清单' : '先检查导入清单',
          onPrimary: referenceOnly ? startWithReferences : reportReady ? report : () => {
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

  function showImportDone(result, count, reportReady, hasSpeciesReferences, referenceOnly) {
    const box = $('pj-import-done');
    if (!box) return;
    box.hidden = false;
    box.innerHTML = `<b>导入完成：</b>${VCS.esc(result.project_name || val('pj-import-name'))}，` +
      `共 ${count} 个结果。${referenceOnly ? 'Li-S 参考能库已就绪。' : reportReady
        ? 'ΔE 已显示在下方，可以生成报告。' : '请先处理下方 ΔE 表中的缺项。'}` +
      '<div class="actions">' + (referenceOnly ? ''
        : '<button class="btn" type="button" data-next="delta">重新计算 ΔE</button>') +
      (hasSpeciesReferences
        ? '<button class="btn primary" type="button" data-next="lis">用这些参考能开始吸附计算</button>' : '') +
      (referenceOnly ? '' : `<button class="btn primary" type="button" data-next="report"${reportReady ? '' : ' disabled ' +
        'title="ΔE 或收敛状态仍有缺项，暂不生成报告"'}>生成完整报告</button>`) + '</div>';
    const deltaButton = box.querySelector('[data-next="delta"]');
    if (deltaButton) deltaButton.addEventListener('click', delta);
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

  function guessSpecies(path, suggested) {
    const refs = selectedReferenceSpecies();
    const exact = refs.find(x => x.toLowerCase() === String(suggested || '').trim().toLowerCase());
    if (exact) return exact;
    const name = pathBase(path).toLowerCase().replace(/[^a-z0-9]/g, '');
    return refs.slice().sort((a, b) => b.length - a.length)
      .find(x => name.includes(x.toLowerCase().replace(/[^a-z0-9]/g, ''))) || String(suggested || '').trim();
  }

  function lisConfigItems() {
    return State.configs.map(path => ({ path, species: String(State.configSpecies[path] || '').trim() }));
  }

  function lisContentFingerprint() {
    return JSON.stringify({
      name: val('pj-name'), root: val('pj-root'), slab: val('pj-slab'), incar: val('pj-incar'),
      reference: val('lis-reference'), configs: lisConfigItems(),
    });
  }

  function reusablePreparedLis() {
    return State.preparedLis && State.preparedLis.fingerprint === lisContentFingerprint()
      ? State.preparedLis : null;
  }

  function methodConfirmationPayload() {
    return {
      confirmed: !!($('lis-method-confirm') && $('lis-method-confirm').checked),
      reason: val('lis-method-reason'),
    };
  }

  function methodCheckStatus(check) {
    const value = String(check && (check.status || check.state || check.result) || '').toLowerCase();
    if (['incompatible', 'blocked', 'fail', 'failed'].includes(value)) return 'incompatible';
    if (['unverified', 'unknown', 'needs_confirmation', 'review'].includes(value)) return 'unverified';
    if (['compatible', 'verified', 'pass', 'passed', 'ok'].includes(value)) return 'compatible';
    return '';
  }

  function renderMethodCheck(check, needsConfirmation, confirmedAccepted) {
    const box = $('lis-method-check');
    if (!box) return;
    if (!check && needsConfirmation) {
      check = { status: 'unverified', reasons: ['后端要求人工确认，但未提供完整的方法比较明细'] };
    }
    const fingerprint = lisContentFingerprint();
    const isNewCheck = !!check && State.methodCheckFingerprint !== fingerprint;
    State.methodCheck = check || null;
    State.methodCheckFingerprint = check ? fingerprint : '';
    if (isNewCheck && !confirmedAccepted) {
      const confirm = $('lis-method-confirm');
      const reason = $('lis-method-reason');
      if (confirm) confirm.checked = false;
      if (reason) reason.value = '';
    }
    State.needsMethodConfirmation = !!needsConfirmation ||
      (methodCheckStatus(check) === 'unverified' && !confirmedAccepted);
    const status = methodCheckStatus(check);
    if (!check || status === 'compatible') {
      box.hidden = true;
      box.className = 'lis-method-check';
      updateLisReadiness();
      return;
    }
    const reasons = [
      ...textList(check.issues), ...textList(check.reasons), ...textList(check.warnings),
      ...textList(check.details), ...textList(check.summary),
    ];
    box.hidden = false;
    box.className = 'lis-method-check ' + (status === 'incompatible' ? 'bad' : 'warn');
    const title = $('lis-method-title');
    if (title) title.textContent = status === 'incompatible'
      ? '计算方法不兼容，已阻止提交'
      : '参考能与新任务的方法信息无法自动完全核验';
    const list = $('lis-method-reasons');
    if (list) list.innerHTML = (reasons.length ? reasons : ['缺少足够的方法元数据，不能自动证明能量可直接比较'])
      .map(item => `<li>${VCS.esc(item)}</li>`).join('');
    box.querySelectorAll('.lis-method-confirm,.lis-method-reason-label').forEach(el => {
      el.hidden = status === 'incompatible';
    });
    updateLisReadiness();
  }

  function invalidatePreparedLis() {
    const prepared = State.preparedLis;
    const fingerprint = lisContentFingerprint();
    if (State.methodCheck && State.methodCheckFingerprint !== fingerprint) {
      State.methodCheck = null;
      State.methodCheckFingerprint = '';
      State.needsMethodConfirmation = false;
      const methodBox = $('lis-method-check');
      if (methodBox) methodBox.hidden = true;
      const confirm = $('lis-method-confirm');
      const reason = $('lis-method-reason');
      if (confirm) confirm.checked = false;
      if (reason) reason.value = '';
    }
    if (!prepared || prepared.fingerprint === fingerprint) return;
    State.preparedLis = null;
    State.workflowSubmitted = false;
    State.preparedConflictHint = { oldName: prepared.name, path: prepared.path };
    State.methodCheck = null;
    State.methodCheckFingerprint = '';
    State.needsMethodConfirmation = false;
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

  function sameLocalPath(left, right) {
    return String(left || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase() ===
      String(right || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  }

  function removeConfigPath(path) {
    const removed = State.configs.filter(item => sameLocalPath(item, path));
    if (!removed.length) return false;
    State.configs = State.configs.filter(item => !sameLocalPath(item, path));
    removed.forEach(item => { delete State.configSpecies[item]; });
    return true;
  }

  function appendConfig(path, species) {
    const clean = String(path || '').trim();
    if (!clean) return false;
    if (State.configs.includes(clean)) return false;
    State.configs.push(clean);
    State.configSpecies[clean] = guessSpecies(clean, species);
    invalidatePreparedLis();
    return true;
  }

  async function addLisInputDirectory() {
    const picked = await VCS.call('pick_dir');
    if (picked && picked.error) {
      VCS.log('选择本次计算文件夹失败:' + picked.error, 'failc');
      showBundleStatus('bad', '没有选中文件夹。请选择同时包含固定 INCAR、clean slab 和 adsorption 结构的上层目录。');
      return;
    }
    if (!picked || !picked.path) return;
    const button = $('lis-input-dir');
    if (button) { button.disabled = true; button.textContent = '正在识别整套输入…'; }
    showBundleStatus('', '正在只读扫描固定 INCAR、clean slab 和 adsorption 结构…');
    try {
      const result = await VCS.call('proj_scan_lis_inputs', picked.path);
      if (!result || result.ok === false || result.error) {
        const message = (result && result.error) || '未知错误';
        VCS.log('识别本次计算文件夹失败:' + message, 'failc');
        showBundleStatus('bad', '识别失败：' + message + '。原始文件没有被修改，请修正目录后重试。');
        return;
      }
      if (result.incar) setVal('pj-incar', result.incar);
      if (result.clean_slab) {
        setVal('pj-slab', result.clean_slab);
        removeConfigPath(result.clean_slab);
      }
      if (!val('pj-name')) setVal('pj-name', pathBase(picked.path) + '_adsorption');
      if (!val('pj-root')) setVal('pj-root', pathParent(picked.path));
      let added = 0;
      (result.configs || []).forEach(item => {
        const path = typeof item === 'string' ? item : item && item.path;
        const species = typeof item === 'object' && item
          ? (item.species || item.suggested_species || '') : '';
        if (path && !sameLocalPath(path, result.clean_slab) && appendConfig(path, species)) added++;
      });
      invalidatePreparedLis();
      renderConfigs();
      const warnings = textList(result.warnings);
      const filled = [result.incar ? 'INCAR' : '', result.clean_slab ? 'clean slab' : '']
        .filter(Boolean).join('、') || '构型列表';
      const summary = `已自动填写 ${filled}，新增 ${added} 个 adsorption 结构。`;
      showBundleStatus(warnings.length ? 'warn' : 'ok', summary +
        (warnings.length ? ' 还需确认：' + warnings.join('；') : ' 请检查物种映射后继续。'));
      warnings.forEach(message => VCS.log(message, 'warnc'));
      VCS.log(`整套输入识别完成：${summary}`, warnings.length ? 'warnc' : 'okc');
    } finally {
      if (button) { button.disabled = false; button.textContent = '导入本次计算文件夹（推荐）'; }
    }
  }

  function lisGate() {
    const ref = selectedReferenceProject();
    const refs = selectedReferenceSpecies();
    const items = lisConfigItems();
    const invalidSpecies = items.filter(item => !refs.some(x => x.toLowerCase() === item.species.toLowerCase()));
    const cores = Number(val('lis-cores'));
    const walltime = val('lis-walltime');
    const validWalltime = /^\d{1,3}:\d{2}:\d{2}$/.test(walltime) &&
      Number(walltime.split(':')[1]) < 60 && Number(walltime.split(':')[2]) < 60;
    const nameConflict = State.preparedConflictHint &&
      val('pj-name') === String(State.preparedConflictHint.oldName || '');
    const methodStatus = methodCheckStatus(State.methodCheck);
    const methodConfirmation = methodConfirmationPayload();
    const methodBlocked = methodStatus === 'incompatible';
    const methodNeedsConfirmation = State.needsMethodConfirmation || methodStatus === 'unverified';
    const methodReady = !methodBlocked && (!methodNeedsConfirmation ||
      (methodConfirmation.confirmed && !!methodConfirmation.reason));
    const step = {
      1: !!ref && refs.length > 0,
      2: !!val('pj-incar') && !!val('pj-slab') && items.length > 0 && invalidSpecies.length === 0,
      3: !!val('pj-name') && !!val('pj-root') && !nameConflict,
      4: !!val('lis-profile') && Number.isInteger(cores) && cores > 0 && validWalltime,
    };
    let issue = '';
    if (!ref) issue = '第 1 步：请选择已经导入的 Li-S 参考能项目';
    else if (!refs.length) issue = '第 1 步：所选项目没有可用的 Li-S 物种参考能';
    else if (!val('pj-incar')) issue = '第 2 步：请选择整组固定使用的 INCAR';
    else if (!val('pj-slab')) issue = '第 2 步：请选择 clean slab POSCAR';
    else if (!items.length) issue = '第 2 步：至少添加一个 adsorption POSCAR';
    else if (invalidSpecies.length) issue = `第 2 步：有 ${invalidSpecies.length} 个构型的物种不在参考能集合中`;
    else if (!val('pj-name')) issue = '第 3 步：填写项目名';
    else if (!val('pj-root')) issue = '第 3 步：选择输出根目录';
    else if (nameConflict) issue = `第 3 步：旧项目“${State.preparedConflictHint.oldName}”已存在；请改用新项目名`;
    else if (!val('lis-profile')) issue = '第 4 步：选择用于提交的服务器';
    else if (!Number.isInteger(cores) || cores < 1) issue = '第 4 步：核数必须是正整数';
    else if (!validWalltime) issue = '第 4 步：墙时请填写为 HH:MM:SS（例如 24:00:00）';
    else if (methodBlocked) issue = '方法检查不兼容：按红色提示统一 INCAR / 赝势 / 泛函后重新建立项目';
    else if (methodNeedsConfirmation && !methodConfirmation.confirmed) issue = '提交前：请阅读方法检查并勾选人工确认';
    else if (methodNeedsConfirmation && !methodConfirmation.reason) issue = '提交前：请填写可审计的方法一致性确认理由';
    return { ok: Object.values(step).every(Boolean) && methodReady, issue, step, ref, refs, items,
      cores, walltime, methodConfirmation, methodBlocked, methodNeedsConfirmation };
  }

  function updateLisReadiness(requestedStep) {
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
      note.textContent = prepared && !prepared.submitted
        ? `本地项目已经生成。直接重试提交即可，不会重复生成或覆盖：${prepared.path}`
        : gate.ok
          ? `已就绪：将生成 clean slab + ${gate.items.length} 个 adsorption 作业，并在 ${val('lis-profile')} 提交。`
          : gate.issue;
    }
    const button = $('pj-submit-all');
    if (button) {
      const prepared = reusablePreparedLis();
      button.disabled = State.lisBusy || !gate.ok || !!(prepared && prepared.submitted);
      if (!State.lisBusy) {
        button.textContent = prepared && prepared.submitted
          ? '整组已提交，自动托管运行中'
          : prepared ? '重试未提交成员（不会重复生成）'
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
      if (!State.configSpecies[path]) State.configSpecies[path] = guessSpecies(path, '');
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

  // 构型列表兼容旧的 string[]，额外维护 path -> species 映射供 proj_prepare_lis。
  function renderConfigs() {
    const box = $('pj-cfg-list');
    const count = $('lis-config-count');
    const refs = selectedReferenceSpecies();
    const validSpecies = path => refs.some(value => value.toLowerCase() ===
      String(State.configSpecies[path] || '').trim().toLowerCase());
    const matched = State.configs.filter(validSpecies).length;
    if (count) count.textContent = State.configs.length
      ? `已匹配 ${matched}/${State.configs.length} 个构型` : '尚未添加构型';
    updateBulkSpeciesOptions(refs);
    if (!box) return;
    if (!State.configs.length) {
      box.innerHTML = '<div class="pj-cfgempty">尚未添加 adsorption POSCAR；可逐个添加，也可一次扫描整个文件夹。</div>';
      updateLisReadiness();
      return;
    }
    const onlyUnmatched = !!($('lis-only-unmatched') && $('lis-only-unmatched').checked);
    const rows = State.configs.map((path, index) => ({ path, index }))
      .filter(item => !onlyUnmatched || !validSpecies(item.path));
    if (!rows.length) {
      box.innerHTML = '<div class="pj-cfgempty">所有构型都已匹配参考物种。</div>';
      updateLisReadiness();
      return;
    }
    box.innerHTML = rows.map(({ path: p, index: i }) => {
      const species = String(State.configSpecies[p] || '');
      const valid = validSpecies(p);
      const speciesCheck = valid ? '已匹配参考能' : `请选择：${refs.join('、') || '先选参考项目'}`;
      const speciesOptions = '<option value="">请选择对应物种</option>' +
        (!valid && species ? `<option value="${VCS.esc(species)}" selected>${VCS.esc(species)}（未匹配）</option>` : '') +
        refs.map(value => `<option value="${VCS.esc(value)}"${value.toLowerCase() === species.trim().toLowerCase() ? ' selected' : ''}>${VCS.esc(value)}</option>`).join('');
      return `<div class="pj-cfgrow lis-cfgrow${valid ? '' : ' invalid'}">` +
        `<div class="lis-cfgpath"><span class="path" title="${VCS.esc(p)}">${VCS.esc(pathBase(p))}</span>` +
        `<span class="sub" title="${VCS.esc(p)}">${VCS.esc(p)}</span></div>` +
        `<label>对应物种<select class="ipt lis-species" data-species-index="${i}">${speciesOptions}</select></label>` +
        `<span class="lis-species-check">${VCS.esc(speciesCheck)}</span>` +
        `<button class="btn quiet" type="button" data-rm="${i}">移除</button></div>`;
    }).join('');
    box.querySelectorAll('select[data-species-index]').forEach(input => {
      input.addEventListener('change', () => {
        const path = State.configs[Number(input.dataset.speciesIndex)];
        if (!path) return;
        State.configSpecies[path] = exactReferenceSpecies(input.value) || input.value.trim();
        invalidatePreparedLis();
        renderConfigs();
      });
    });
    box.querySelectorAll('button[data-rm]').forEach(btn => {
      btn.addEventListener('click', () => {
        const path = State.configs[Number(btn.dataset.rm)];
        State.configs.splice(Number(btn.dataset.rm), 1);
        if (path) delete State.configSpecies[path];
        invalidatePreparedLis();
        renderConfigs();
      });
    });
    updateLisReadiness();
  }

  function applyBulkSpecies() {
    const species = exactReferenceSpecies(val('lis-bulk-species'));
    if (!species) {
      VCS.toast('请先从参考物种中选择一个值', 'fail');
      return;
    }
    const refs = selectedReferenceSpecies().map(value => value.toLowerCase());
    let changed = 0;
    State.configs.forEach(path => {
      const current = String(State.configSpecies[path] || '').trim().toLowerCase();
      if (!refs.includes(current)) {
        State.configSpecies[path] = species;
        changed++;
      }
    });
    invalidatePreparedLis();
    renderConfigs();
    VCS.toast(changed ? `已为 ${changed} 个未匹配构型设置 ${species}` : '所有构型已经匹配，无需修改');
  }

  async function addConfig() {
    const r = await VCS.call('pick_file', 'poscar');
    if (r && r.error) { VCS.log('选择构型失败:' + r.error, 'failc'); return; }
    if (r && r.path) {
      if (!appendConfig(r.path, '')) {
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
      if (base === 'contcar') return 20;
      if (base === 'poscar') return 10;
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
      // 同目录优先后端显式 preferred；否则采用可解析的最终 CONTCAR，缺失/无效才回退 POSCAR。
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
    const picked = await VCS.call('pick_dir');
    if (picked && picked.error) {
      VCS.log('选择构型文件夹失败:' + picked.error, 'failc');
      showConfigScanStatus('bad', '没有选中结构文件夹。请重新选择包含 POSCAR / CONTCAR / .vasp 文件的上层目录。');
      return;
    }
    if (!picked || !picked.path) return;
    if (!val('pj-name')) setVal('pj-name', pathBase(picked.path) + '_adsorption');
    if (!val('pj-root')) setVal('pj-root', pathParent(picked.path));
    const button = $('pj-cfg-dir');
    if (button) { button.disabled = true; button.textContent = '正在扫描结构…'; }
    showConfigScanStatus('', '正在递归寻找 POSCAR / CONTCAR / .vasp 结构文件…');
    VCS.log('正在递归扫描 adsorption POSCAR:' + picked.path + '…');
    try {
      const r = await VCS.call('proj_scan_structures', picked.path);
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
        if (appendConfig(path, species)) added++;
      });
      textList(r.warnings).forEach(x => VCS.log(x, 'warnc'));
      renderConfigs();
      VCS.log(`结构扫描完成：新增 ${added} 个，重复或无效跳过 ${raw.length - added} 个`, added ? 'okc' : 'warnc');
      if (!raw.length) {
        showConfigScanStatus('bad', '没有找到结构文件。请确认文件名为 POSCAR / CONTCAR，或扩展名为 .vasp / .poscar。');
        VCS.toast('没有找到 POSCAR / CONTCAR 结构文件', 'fail');
      } else {
        const choice = preferred.suppressed
          ? `；同目录冲突时已自动选择可用的最终 CONTCAR（跳过 ${preferred.suppressed} 个重复文件）` : '';
        const finalNote = preferred.finalStructures
          ? `；已选 ${preferred.finalStructures} 个最终 CONTCAR，如需原始 POSCAR 请使用“逐个添加”` : '';
        showConfigScanStatus('ok', `已新增 ${added} 个结构${choice}${finalNote}。请逐行确认“对应物种”，红色项必须修正后才能提交。`);
      }
    } finally {
      if (button) { button.disabled = false; button.textContent = '导入整个结构文件夹'; }
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
    if (/INCAR|POSCAR|结构文件|文件不存在|无法读取/.test(text)) return '文件路径已经失效或不可读。回到第 2 步重新选择对应文件。';
    if (/方法|核验|确认理由|不能直接相减/.test(text)) return '阅读页面内的方法检查。明确不兼容时统一泛函、赝势、ENCUT 等设置；证据不完整时须人工核对并填写理由。';
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
    if (/方法|核验|确认理由|不能直接相减/.test(text)) return ['method', '查看方法检查'];
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
    const gate = lisGate();
    updateLisReadiness();
    if (!gate.ok) { VCS.toast(gate.issue, 'fail'); return; }
    const reusable = reusablePreparedLis();
    const button = $('pj-submit-all');
    const resultBox = $('lis-submit-result');
    State.lisBusy = true;
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
          val('pj-incar'), val('pj-root'), gate.ref.path, gate.methodConfirmation);
        const methodCheck = prepared && prepared.method_check;
        const needsMethod = !!(prepared && prepared.needs_method_confirmation);
        const acceptedMethod = gate.methodConfirmation.confirmed && !!gate.methodConfirmation.reason && !needsMethod;
        if (methodCheck || needsMethod) renderMethodCheck(methodCheck, needsMethod, acceptedMethod);
        if (methodCheckStatus(methodCheck) === 'incompatible') {
          const error = (prepared && prepared.error) || '参考能与新任务的方法不兼容';
          VCS.log('方法检查阻止提交:' + error, 'failc');
          showLisFailure('方法不兼容，未生成项目', error, 'prepare');
          return;
        }
        if (needsMethod) {
          VCS.log('方法信息需要人工确认；请阅读页面内检查结果并填写确认理由', 'warnc');
          showLisFailure('需要确认计算方法后才能继续',
            '请阅读上方方法检查，勾选确认并填写理由，然后再次点击主按钮。', 'prepare');
          return;
        }
        if (!prepared || prepared.ok === false || prepared.error) {
          const error = (prepared && prepared.error) || '未知错误';
          if (!methodCheck && /方法.*(?:不一致|不兼容)|不能直接相减/.test(error)) {
            renderMethodCheck({ status: 'incompatible', issues: [error] }, false, false);
          }
          VCS.log('Li-S 项目生成失败:' + error, 'failc');
          textList(prepared && prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
          showLisFailure('项目生成未完成', error, 'prepare');
          return;
        }
      }
      textList(prepared.advisories).forEach(x => VCS.log('方法学提示:' + x, 'warnc'));
      textList(prepared.warnings).forEach(x => VCS.log(x, 'warnc'));
      const projectPath = projectPathFrom(prepared);
      if (!projectPath) {
        VCS.log('Li-S 项目生成失败:后端没有返回项目路径', 'failc');
        showLisFailure('项目生成未完成', '后端没有返回项目路径', 'prepare');
        return;
      }
      if (!reusable) {
        State.preparedLis = {
          path: projectPath, fingerprint: lisContentFingerprint(), name: val('pj-name'), submitted: false,
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
    if ((State.workflowSubmitted || State.workflowNeedsHuman) && !State.workflowResultReady) {
      if (typeof VCS.navigate === 'function') await VCS.navigate('jobs', { source: 'adsorption-journey' });
      return;
    }
    await startLiS();
  }

  // ── 批量生成:proj_create → advisories/warnings 逐条 warn 级 log → 成功刷台账 ──
  async function create() {
    const btn = $('pj-create');
    const name = val('pj-name'), slab = val('pj-slab'), incar = val('pj-incar');
    const gas = val('pj-gas'), root = val('pj-root');
    const configs = State.configs.slice();
    if (btn) btn.disabled = true;
    VCS.log('批量生成:清洁表面 + ' + configs.length + ' 构型' + (gas ? ' + 气相参考' : '') + '…');
    try {
      const r = await VCS.call('proj_create', name, slab, configs, incar, gas, root);
      if (!r || r.ok === false || r.error) {
        VCS.log('批量生成失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      // 后端已把 advisories 与 warnings 拍平为字符串列表;都用 warn 级 log
      (r.advisories || []).forEach(a => VCS.log('方法学提示:' + a, 'warnc'));
      (r.warnings || []).forEach(w => VCS.log(w, 'warnc'));
      VCS.log('已生成项目:' + (r.project_path || ''), 'okc');
      VCS.log('作业已入台账,去作业页上传提交;全部 DONE 后回本页算 ΔE', 'okc');
      // 生成的成员作业进了台账 → 刷新任务页
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      await reloadProjects();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 已有项目:下拉 + 刷新 ──────────────────────────────────────────────────
  async function reloadProjects(preferredReference) {
    const sel = $('pj-select');
    const previous = sel ? sel.value : '';
    const [r, pipeline] = await Promise.all([
      VCS.call('proj_list'), VCS.call('pipeline_status'),
    ]);
    const pipelineByPath = new Map(((pipeline && pipeline.projects) || [])
      .map(item => [item.path, item]));
    State.projects = ((r && r.projects) || []).map(project => {
      const progress = pipelineByPath.get(project.path) || {};
      return Object.assign({}, project, {
        pipeline_stage: progress.stage || '',
        pipeline_needs_human: !!progress.needs_human,
        pipeline_recover_round: Number(progress.recover_round || 0),
        n_done: progress.done != null ? Number(progress.done) : project.n_done,
        n_members: progress.total != null ? Number(progress.total) : project.n_members,
      });
    });
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
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(p.name || '(未命名)'));
      box.appendChild(lab);
    });
  }

  function logFigResult(r, what) {
    if (!r || r.ok === false || r.error) {
      VCS.log(what + '失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    (r.files || []).forEach(f => VCS.log('已生成:' + f, 'okc'));
    (r.skipped || []).forEach(s => VCS.log('跳过 ' + s.kind + ':' + s.reason, 'warnc'));
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
    const box = $('fig-projlist');
    const paths = box
      ? Array.from(box.querySelectorAll('input:checked')).map(cb => cb.dataset.path)
      : [];
    if (paths.length < 2) { VCS.log('多项目对比请勾选至少 2 个项目', 'failc'); return; }
    const kinds = [];
    if ($('fig-heatmap') && $('fig-heatmap').checked) kinds.push('heatmap');
    if ($('fig-scaling') && $('fig-scaling').checked) kinds.push('scaling');
    if ($('fig-volcano') && $('fig-volcano').checked) kinds.push('volcano');
    if (!kinds.length) { VCS.log('请至少勾选一种对比图', 'failc'); return; }
    const btn = $('pj-cmpfigs');
    if (btn) btn.disabled = true;
    VCS.log('对比出图中(' + paths.length + ' 个项目,' + kinds.join('/') + ')…');
    try {
      const r = await VCS.call('proj_compare_figures', paths, kinds, null);
      logFigResult(r, '对比出图');
    } finally {
      if (btn) btn.disabled = false;
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
    const complete = !!(rows && rows.length && rows.every(row => row.delta_e != null));
    const missing = rows ? rows.filter(row => row.delta_e == null).length : null;
    const pipelineStage = String(project.pipeline_stage || project.autopilot_stage || '').trim();
    let stage;
    let next;
    if (complete) {
      stage = 'ΔE 已完整'; next = '下一步：核对下表后生成完整 HTML 报告。';
    } else if (missing != null && missing > 0) {
      stage = `${missing} 个构型尚缺可靠 ΔE`;
      next = '下一步：保持软件运行等待自动下载/续算，完成后点“刷新并计算 ΔE”。';
    } else if (done != null && total && done < total) {
      stage = `自动运行中 ${done}/${total}`;
      next = '下一步：保持软件运行；任务完成后回来刷新 ΔE。';
    } else if (!total && refs.length) {
      stage = '参考能库已就绪'; next = '下一步：用这些参考能开始新的 slab / adsorption 计算。';
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
    if (reportButton) reportButton.disabled = !complete;
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
    const rows = r.rows || [];
    State.workflowResultReady = !!(rows.length && rows.every(row => row.delta_e != null));
    updateProjectSummary(r);
    updateJourney();
    if (State.workflowResultReady) VCS.toast('ΔE 已完整；下一步生成完整报告');
    return r;
  }

  function fmt(x, digits) {
    return (typeof x === 'number' && isFinite(x)) ? x.toFixed(digits) : '—';
  }

  function deltaRepair(row) {
    const note = String(row && row.note || '');
    if (row && row.delta_e != null) return '';
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

  // ── 生成完整报告:pick_dir + 默认文件名 → proj_report(耗时长,按钮禁用) ──
  async function report() {
    const proj = currentProject();
    if (!proj) return;
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;   // 用户取消
    const save = joinPath(dr.path, (proj.name || 'project') + '_完整报告.html');
    const btn = $('pj-report');
    if (btn) btn.disabled = true;
    VCS.log('生成完整报告(Origin 图表 + 结构图 + AI 分析),生成中,可能需要几分钟…');
    try {
      const r = await VCS.call('proj_report', proj.path, save);
      if (!r || r.ok === false || r.error) {
        VCS.log('生成完整报告失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      VCS.log('完整报告已生成:' + (r.file || save), 'okc');
      VCS.call('open_dir', r.file || save);     // 输出反馈统一:打开所在目录
      VCS.toast('报告已生成');
    } finally {
      if (btn) btn.disabled = false;
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
    wire('pj-slab-btn', () => pickInto('pj-slab', 'poscar').then(path => {
      if (path && removeConfigPath(path)) renderConfigs();
      invalidatePreparedLis(); updateLisReadiness();
    }));
    wire('pj-incar-btn', () => pickInto('pj-incar', 'incar').then(() => {
      invalidatePreparedLis(); updateLisReadiness();
    }));
    wire('pj-gas-btn', () => pickInto('pj-gas', 'poscar'));
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
    const methodConfirm = $('lis-method-confirm');
    if (methodConfirm) methodConfirm.addEventListener('change', updateLisReadiness);
    const methodReason = $('lis-method-reason');
    if (methodReason) methodReason.addEventListener('input', updateLisReadiness);
    const profile = $('lis-profile');
    if (profile) profile.addEventListener('change', () => {
      try { localStorage.setItem('vcs.jobs.profile', profile.value); } catch (e) { /* 不阻塞 */ }
      applyLisProfileDefaults();
    });
    ['pj-name', 'pj-slab', 'pj-incar', 'pj-root'].forEach(id => {
      const el = $(id); if (el) el.addEventListener('input', () => {
        invalidatePreparedLis(); updateLisReadiness();
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
    const projectSelect = $('pj-select');
    if (projectSelect) projectSelect.addEventListener('change', () => {
      restoreWorkflowState(State.projects.find(p => p.path === projectSelect.value));
      try { localStorage.setItem(CURRENT_PROJECT_KEY, projectSelect.value); } catch (_) { /* 不阻塞 */ }
      updateProjectSummary(); updateJourney();
    });
    wire('pj-figs', makeFigures);
    wire('pj-cmpfigs', makeCompareFigures);
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

  // 任务页组头「算 ΔE」调用:刷新项目列表后按项目名选中(找不到则保持默认)
  async function selectByName(name) {
    await reloadProjects();
    const hit = State.projects.find(p => p.name === name);
    const sel = $('pj-select');
    if (hit && sel) { sel.value = hit.path; updateProjectSummary(); }
  }

  // 切回项目页时刷新项目下拉
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'project') { reloadProjects(); loadLisProfiles(); }
  });
  document.addEventListener('vcs:scenario', e => applyProjectMode(e.detail && e.detail.scenario));
  document.addEventListener('vcs:calculation', applyProjectCalculation);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Project = { reload: reloadProjects, selectByName, openImport, startLiS };
})();
