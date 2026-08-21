// dashboard.js — 仪表盘首页:作业状态汇总 / 最近活动 / 快捷入口 / 待办。
// 纯前端组合现有桥方法(list_jobs / proj_list / list_profiles),不新增后端调用面。
// 只依赖 app.js 暴露的 VCS.*;全部插值走 VCS.esc;零 emoji;中文文案。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const tr = (key, fallback, params) => typeof VCS.t === 'function'
    ? VCS.t(key, params || {}, fallback) : fallback;

  const QUEUE_STATES = ['QUEUED', 'SUBMITTED', 'UPLOADED'];
  const NEED_STATES = ['FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'];
  const PROJECT_IMPORT_DRAFT_ID = 'project-import';
  const CLUSTER_PROFILE_DRAFT_ID = 'cluster-profile';

  function count(jobs, states) {
    return jobs.filter(j => states.indexOf(j.state) >= 0).length;
  }

  // 跳页统一经 VCS.navigate：workspace 壳层会在这里完成 hash、
  // 未保存拦截和旧页适配；不再依赖某个可见 nav link 存在。
  function navTo(page, options = {}) {
    return VCS.navigate(page, Object.assign({ source: 'dashboard' }, options));
  }

  async function openResultsImport() {
    const out = await VCS.navigate('project', { source: 'dashboard-import-results' });
    if (!out.ok) return;
    if (window.Project && typeof window.Project.openImport === 'function') {
      await window.Project.openImport();
      return;
    }
    VCS.toast('请在“吸附能项目”中选择“导入整个文件夹”');
  }

  async function openInputsSubmit() {
    const out = await navTo('jobs', { source: 'dashboard-submit-inputs' });
    if (!out || !out.ok) return;
    // 导航与手风琴初始化都是同步完成；下一帧聚焦到快速提交首步。
    window.setTimeout(() => {
      const card = $('quick-submit-card');
      if (!card) return;
      if (card.getAttribute('data-open') !== '1') {
        const head = card.querySelector(':scope > .acc-h');
        if (head) head.click();
      }
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const first = $('qs-add-dir');
      if (first) first.focus();
    }, 0);
  }

  const ACTIONS = {
    import_adsorption_results: ['导入已算好的 Li-S 结果', '整文件夹识别参考态、slab 与吸附构型', openResultsImport],
    lis_new: ['Li-S 一站式：开始新的吸附计算', '参考能 + 各目录 POSCAR/INCAR → 自动托管整组任务', async () => {
      const out = await VCS.navigate('project', { source: 'dashboard-lis-new' });
      if (out.ok && window.Project && Project.startLiS) Project.startLiS();
    }],
    submit_inputs: ['通用快速提交：已有四件套', '整文件夹预检并批量提交；不会自动建立 Li-S 吸附能项目', openInputsSubmit],
    continue_jobs: ['继续已有任务', '监控、续算、下载与失败恢复', () => navTo('jobs')],
    new_structure: ['从结构开始', '建模或打开结构文件', () => navTo('structure')],
    choose_vasp_task: ['选择本次 DFT 计算', '按设置中的类型只显示对应参数与下一步', () =>
      VCS.navigate('generate', { source: 'dashboard-task', focusSelector: '#taskcat-card' })],
    analyze_vasp_result: ['解析已有计算结果', '能带、EOS、功函数、收敛与性质计算', async () => {
      const out = await VCS.navigate('project', { source: 'dashboard-analysis', focusSelector: '#taskana-card' });
      const sel = $('analysis-type');
      if (out.ok && sel) { sel.value = 'taskana'; sel.dispatchEvent(new Event('change', { bubbles: true })); }
    }],
    build_molecule: ['建立或导入分子', '图片 / SMILES / 文件 → 3D 分子结构', async () => {
      const out = await VCS.navigate('structure', { source: 'dashboard-molecule' });
      if (!out.ok) return;
      const b = document.querySelector('[data-seg="data-src"] [data-seg-val="molecular"]');
      if (b) b.click();
    }],
    gaussian_input: ['生成 Gaussian 输入', '分子结构 → 方法、基组与服务器资源', async () => {
      const out = await VCS.navigate('generate', { source: 'dashboard-gaussian' });
      if (out.ok && window.Generate && Generate.selectEngine) Generate.selectEngine('gaussian');
    }],
    wavefunction: ['波函数分析', 'Multiwfn / VMD 分析与可视化', () => navTo('wavefunction')],
  };

  let startRenderVersion = 0;

  async function renderStartActions(sc) {
    const box = $('db-start-actions');
    if (!box || !sc) return;
    const version = ++startRenderVersion;
    let keys = (sc.home_actions || []).filter(k => ACTIONS[k]);
    const active = VCS.activeCalculation || '';
    // Li-S 默认吸附项目保留“导入 / 一站式 / 四件套 / 继续”四入口；用户一旦
    // 明确改选其它 DFT 类型，首页第一项就变成该类型的真实入口，不再误导回吸附表单。
    if (active && sc.key !== 'molecular' && !(sc.key === 'lis' && active === 'adsorption_project')) {
      const r = await VCS.call('task_catalog', sc.key || null, active, VCS.activeEngine || 'vasp');
      if (version !== startRenderVersion) return;
      const task = r && r.ok !== false && (r.tasks || [])[0];
      if (task) {
        const route = VCS.calculationRoute && VCS.calculationRoute(active, sc);
        const isEnglish = VCS.i18n && VCS.i18n.lang === 'en';
        const verb = route && route.page === 'project'
          ? tr('dashboard.action.open_result_tool', '打开结果工具')
          : tr('dashboard.action.start_preparing', '开始准备');
        const engine = String(VCS.activeEngine || 'vasp').toUpperCase();
        const taskName = isEnglish ? (task.name_en || task.key) : (task.name_zh || task.key);
        const requires = isEnglish
          ? (task.requires_en || 'Follow the task-specific input requirements')
          : (task.requires || '按提示准备输入');
        const outputs = isEnglish
          ? (task.outputs_en || 'calculation results') : (task.outputs || '计算结果');
        ACTIONS.selected_calculation = [
          tr('dashboard.action.selected_title', '{verb}：{engine} · {task}', {
            verb, engine, task: taskName,
          }),
          tr('dashboard.action.selected_contract', '{requires} → {outputs}', {
            requires, outputs,
          }),
          () => VCS.openCalculation
            ? VCS.openCalculation(active, { source: 'dashboard-selected-calculation' })
            : navTo((route && route.page) || 'generate'),
        ];
        keys = ['selected_calculation', 'submit_inputs', 'continue_jobs', 'analyze_vasp_result']
          .filter(k => ACTIONS[k]);
      }
    }
    box.innerHTML = keys.map((key, i) => {
      const a = ACTIONS[key];
      return `<button class="start-action${i < 2 ? ' primary' : i > 2 ? ' quiet' : ''}" data-action="${VCS.esc(key)}">` +
        `<b>${VCS.esc(a[0])}</b><span>${VCS.esc(a[1])}</span></button>`;
    }).join('');
    const title = document.querySelector('#db-start-here .start-copy h2');
    const sub = document.querySelector('#db-start-here .start-copy p');
    if (title) title.textContent = sc.key === 'lis' && active === 'adsorption_project'
      ? '这次要导入结果，还是开始新的吸附计算？' :
      sc.key === 'molecular' ? '这次从分子结构、Gaussian 输入还是已有任务开始？' :
        active ? '按本次计算类型继续' : '这次从哪一步开始？';
    if (sub) {
      const scenarioName = VCS.i18n && VCS.i18n.lang === 'en'
        ? (sc.name_en || sc.key) : (sc.name || sc.key);
      sub.textContent = tr('dashboard.scenario.summary',
        '当前工作模式：{scenario}。这里只保留本次需要的入口，可随时在设置中切换。', {
          scenario: scenarioName,
        });
    }
  }

  // ── 卡片 1:状态汇总大数字(点击跳作业页) ─────────────────────────────────
  function renderNums(jobs) {
    const box = $('db-nums');
    if (!box) return;
    const items = [
      ['run', '运行中', count(jobs, ['RUNNING'])],
      ['q', '排队中', count(jobs, QUEUE_STATES)],
      ['ok', '已完成', count(jobs, ['DONE'])],
      ['fail', '需处理', count(jobs, NEED_STATES)],
    ];
    box.innerHTML = items.map(([cls, label, n]) => {
      const aria = tr('dashboard.jobs.summary_aria', '{label} {count}，查看作业页', {
        label, count: n,
      });
      const title = tr('dashboard.jobs.open', '查看作业页');
      return `<div class="db-num ${cls}" data-goto="jobs" role="link" tabindex="0" ` +
        `aria-label="${VCS.esc(aria)}" title="${VCS.esc(title)}">` +
        `<b>${n}</b><span>${VCS.esc(label)}</span></div>`;
    }).join('');
  }

  // ── 卡片 2:最近活动(按更新时间倒序取前 6 个作业;体系名加元素徽章) ─────────
  function renderRecent(jobs) {
    const box = $('db-recent');
    if (!box) return;
    const rows = jobs.slice()
      .sort((a, b) => String(b.updated || '').localeCompare(String(a.updated || '')))
      .slice(0, 6);
    if (!rows.length) {
      box.innerHTML = '<div class="db-empty">还没有纳管的作业 — 从「生成输入」开始</div>';
      return;
    }
    box.innerHTML = rows.map(r => {
      const name = r.name || tr('dashboard.jobs.unnamed', '未命名作业');
      const state = r.state || tr('dashboard.jobs.state_unknown', '状态未知');
      const aria = tr('dashboard.jobs.row_aria', '{name}，{state}，查看作业页', {
        name, state,
      });
      return `<div class="db-row" data-goto="jobs" role="link" tabindex="0" ` +
        `aria-label="${VCS.esc(aria)}" title="${VCS.esc(tr('dashboard.jobs.open', '查看作业页'))}">` +
        VCS.elementBadge(r.name) +
        `<span class="name">${VCS.esc(r.name)}</span>` +
        (r.project ? `<span class="db-proj">${VCS.esc(r.project)}</span>` : '') +
        '<span class="sp"></span>' + VCS.pill(r.state) +
        `<span class="db-time">${VCS.esc(r.updated || '')}</span></div>`;
    }).join('');
  }

  // ── 卡片:项目管线(每项目一行站点进度;当前站高亮,NEEDS_HUMAN 红点) ─────────
  const STAGE_LABEL = { generate: '生成', submit: '提交', monitor: '监控',
    recover: '恢复', analysis: '分析', report_done: '报告产物' };
  const ACTIVE_PIPELINE_STAGES = new Set(['generate', 'submit', 'monitor', 'recover', 'analysis']);

  function portfolioStatus(value) {
    return String(value == null ? '' : value).trim().toLowerCase();
  }

  // Every value uses the exact same registered-project denominator.  Rows are
  // only the successfully read server facts, so degraded responses can retain
  // honest lower-bound counts without silently shrinking that denominator.
  // Categories deliberately overlap.  report_reason is explanatory text and
  // report_done is only a workflow stage; neither is scientific-final evidence.
  function summarizeProjectPortfolio(projects, registeredTotal = null) {
    const rows = Array.isArray(projects) ? projects : [];
    const declaredTotal = registeredTotal == null ? null : Number(registeredTotal);
    const denominator = Number.isInteger(declaredTotal) && declaredTotal >= rows.length
      ? declaredTotal : rows.length;
    const summary = {
      projects: denominator,
      active_pipeline: 0,
      needs_human: 0,
      publication_gate_blocked: 0,
      eligible_final: 0,
      human_scientific_reviewed_final: 0,
    };
    rows.forEach(project => {
      const row = project && typeof project === 'object' ? project : {};
      if (ACTIVE_PIPELINE_STAGES.has(portfolioStatus(row.stage))) summary.active_pipeline += 1;
      if (row.needs_human === true) summary.needs_human += 1;
      const gate = portfolioStatus(row.publication_gate_status);
      if (gate === 'blocked') summary.publication_gate_blocked += 1;
      if (gate === 'eligible') summary.eligible_final += 1;
      if (row.artifact_current === true &&
          portfolioStatus(row.artifact_status) === 'ready' &&
          row.scientific_stale === false && gate === 'eligible' &&
          portfolioStatus(row.scientific_status) === 'final' &&
          portfolioStatus(row.scientific_qualification) === 'human_scientific_reviewed') {
        summary.human_scientific_reviewed_final += 1;
      }
    });
    return summary;
  }

  function portfolioStateMarkup(state, projects, error, metadata = {}) {
    if (state === 'loading') {
      return '<section class="project-portfolio state" data-state="loading" aria-live="polite">' +
        `<b>${VCS.esc(tr('dashboard.portfolio.title', '实验室 Portfolio'))}</b>` +
        `<span>${VCS.esc(tr('dashboard.portfolio.loading', '正在读取项目事实…'))}</span></section>`;
    }
    if (state === 'unavailable') {
      return '<section class="project-portfolio state unavailable" data-state="unavailable" aria-live="polite">' +
        `<b>${VCS.esc(tr('dashboard.portfolio.title', '实验室 Portfolio'))}</b>` +
        `<span>${VCS.esc(tr('dashboard.portfolio.unavailable',
          'Portfolio 暂不可用：{error}', { error: error || tr('common.unknown', '未知错误') }))}</span></section>`;
    }
    const summary = summarizeProjectPortfolio(projects, metadata.registered_total);
    if (!summary.projects) {
      return '<section class="project-portfolio state empty" data-state="empty" aria-live="polite">' +
        `<b>${VCS.esc(tr('dashboard.portfolio.title', '实验室 Portfolio'))}</b>` +
        `<span>${VCS.esc(tr('dashboard.portfolio.empty', '尚无项目，暂无 Portfolio 计数。'))}</span></section>`;
    }
    const total = summary.projects;
    const metrics = [
      ['active_pipeline', 'dashboard.portfolio.active', '活跃管线', 'project-workflow', ''],
      ['needs_human', 'dashboard.portfolio.needs_human', '需人工介入', 'run-jobs', 'need'],
      ['publication_gate_blocked', 'dashboard.portfolio.blocked', '发布门禁阻断', 'publish-report', ''],
      ['eligible_final', 'dashboard.portfolio.eligible', '最终版资格通过', 'publish-report', ''],
      ['human_scientific_reviewed_final', 'dashboard.portfolio.human_final',
        '人工科学复核最终版', 'publish-report', ''],
    ];
    const denominator = tr('dashboard.portfolio.denominator', '{count} projects', { count: total });
    const cards = metrics.map(([key, labelKey, fallback, route, filter]) => {
      const countValue = summary[key];
      const label = tr(labelKey, fallback);
      const action = route === 'project-workflow'
        ? tr('dashboard.portfolio.open_pipeline', '打开项目管线')
        : route === 'run-jobs'
          ? tr('dashboard.portfolio.open_jobs', '打开作业页')
          : tr('dashboard.portfolio.open_publish', '打开发布工作台');
      return `<button type="button" class="portfolio-metric" data-portfolio-route="${route}" ` +
        `data-portfolio-filter="${filter}" aria-label="${VCS.esc(`${label} ${countValue} / ${total}；${action}`)}">` +
        `<b data-portfolio-count="${key}">${countValue}<small> / ${total}</small></b>` +
        `<span>${VCS.esc(label)}</span><em>${VCS.esc(action)}</em></button>`;
    }).join('');
    const detail = state === 'degraded'
      ? tr('dashboard.data.partial_failure',
        '部分数据读取失败（下列读数可能不完整）：{errors}', {
          errors: error || 'Some registered project facts are unavailable.',
      })
      : tr('dashboard.portfolio.overlap',
        '五项使用同一项目分母；类别可重叠，不是完成率。');
    const stateClass = state === 'degraded' ? ' degraded' : '';
    return `<section class="project-portfolio${stateClass}" data-state="${state}" aria-live="polite">` +
      '<div class="portfolio-head"><span>' +
      `<b>${VCS.esc(tr('dashboard.portfolio.title', '实验室 Portfolio'))}</b>` +
      `<small>${VCS.esc(denominator)}</small></span>` +
      `<p role="status">${VCS.esc(detail)}</p></div>` +
      `<div class="portfolio-grid">${cards}</div></section>`;
  }

  function navigatePortfolio(route, filter = '') {
    const routeId = String(route || '');
    const query = routeId === 'run-jobs' && filter ? { status: filter } : {};
    if (VCS.workspace && typeof VCS.workspace.navigateRoute === 'function') {
      return VCS.workspace.navigateRoute(routeId, {
        query, source: 'dashboard-portfolio',
      });
    }
    const fallback = routeId === 'run-jobs' ? 'jobs'
      : routeId === 'publish-report' ? 'report-workbench' : 'dashboard';
    return navTo(fallback, { source: 'dashboard-portfolio' });
  }

  function pipelineReportStates(project) {
    const p = project || {};
    const files = p.report_files && typeof p.report_files === 'object'
      ? p.report_files : {};
    const formats = ['html', 'docx', 'pdf'].filter(format => files[format]);
    const artifactRaw = String(
      p.artifact_status || p.report_artifact_status || ''
    ).trim().toLowerCase();
    // report_done 只说明文件生成流程结束，绝不用于推断科学结论是 final。
    const legacyReady = !artifactRaw && (formats.length > 0 || p.stage === 'report_done');
    const artifact = ['ready', 'complete'].includes(artifactRaw) || legacyReady
      ? {
        cls: 'ok',
        label: formats.length
          ? tr('dashboard.report.generated_formats', '已生成（{formats}）', {
            formats: formats.map(value => value.toUpperCase()).join(' · '),
          })
          : '已生成',
      }
      : artifactRaw === 'generated_unrecorded'
        ? { cls: 'warn', label: '已生成但未登记' }
        : artifactRaw === 'stale'
          ? { cls: 'warn', label: '已过期' }
          : artifactRaw === 'failed'
            ? { cls: 'bad', label: '生成失败' }
            : { cls: '', label: '尚未生成' };

    const scienceRaw = String(
      p.scientific_status || p.report_status || p.report_kind || 'pending'
    ).trim().toLowerCase();
    const science = scienceRaw === 'final'
      ? { cls: 'ok', label: '最终' }
      : scienceRaw === 'diagnostic'
        ? { cls: 'warn', label: '诊断' }
        : scienceRaw === 'draft'
          ? { cls: '', label: '草稿' }
          : { cls: '', label: '尚无当前报告' };
    const gateRaw = String(p.publication_gate_status || 'unknown').trim().toLowerCase();
    const gate = gateRaw === 'eligible'
      ? { cls: 'ok', label: '可发布最终版' }
      : gateRaw === 'blocked'
        ? { cls: 'bad', label: '阻断' }
        : gateRaw === 'pending'
          ? { cls: '', label: '等待计算' }
          : { cls: '', label: '未知' };
    const reason = String(p.report_reason || '');
    const desired = String(p.desired_report_kind || '');
    const gateTitle = [reason, desired ? tr('dashboard.report.desired', '目标报告：{kind}', {
      kind: desired,
    }) : ''].filter(Boolean).join('；');
    return '<div class="pl-report-states" aria-label="报告产物状态、科学状态与发布门禁">' +
      `<span class="pl-report product ${artifact.cls}"><b>报告产物</b>${VCS.esc(artifact.label)}</span>` +
      `<span class="pl-report science ${science.cls}"${reason ? ` title="${VCS.esc(reason)}"` : ''}>` +
      `<b>科学状态</b>${VCS.esc(science.label)}</span>` +
      `<span class="pl-report gate ${gate.cls}"${gateTitle ? ` title="${VCS.esc(gateTitle)}"` : ''}>` +
      `<b>发布门禁</b>${VCS.esc(gate.label)}</span></div>`;
  }

  function shortTime(value) {
    if (!value) return '—';
    const d = new Date(value);
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    return String(value);
  }

  function renderAutomation(runtime, err) {
    const box = $('db-automation');
    if (!box) return;
    if (err) {
      box.className = 'db-automation fail';
      box.innerHTML = `<b>自动托管异常</b><span>${VCS.esc(err)}</span>`;
      return;
    }
    const st = runtime || {};
    if (st.last_error) {
      box.className = 'db-automation fail';
      box.innerHTML = `<b>自动托管需检查</b><span>${VCS.esc(st.last_error)}</span>`;
    } else if (st.tick_running) {
      box.className = 'db-automation run';
      box.innerHTML = '<b>自动托管正在运行</b><span>正在同步状态、判断续算、拉回结果或生成报告</span>';
    } else if (st.enabled === false || st.paused) {
      box.className = 'db-automation pause';
      box.innerHTML = '<b>自动托管已暂停</b><span>可在“设置 → 外观与自动化”启用</span>';
    } else if (st.running) {
      const last = shortTime(st.last_finished);
      const next = shortTime(st.next_check);
      box.className = 'db-automation';
      box.innerHTML = `<b>自动托管后台待命</b><span>${VCS.esc(tr(
        'dashboard.automation.schedule', '上次检查 {last} · 下次检查 {next}', { last, next }
      ))}</span>`;
    } else {
      box.className = 'db-automation pause';
      box.innerHTML = '<b>自动托管未启动</b><span>重新打开软件后将自动恢复已保存的托管设置</span>';
    }
  }

  function renderPipeline(projects, err, state = 'ready', metadata = {}) {
    const box = $('db-pipeline');
    if (!box) return;
    const rows = Array.isArray(projects) ? projects : [];
    const responseState = portfolioStatus(metadata.status || state) || 'ready';
    if (responseState === 'loading') {
      box.innerHTML = portfolioStateMarkup('loading');
      return;
    }
    if (responseState === 'unavailable' || (err && responseState !== 'degraded')) {
      box.innerHTML = portfolioStateMarkup('unavailable', null, err) +
        `<div class="pl-empty">${VCS.esc(tr(
        'dashboard.pipeline.read_failed', '读取项目管线失败：{error}', { error: err }
      ))}</div>`; return;
    }
    if (!rows.length) {
      box.innerHTML = portfolioStateMarkup('empty', []) +
        '<div class="pl-empty">暂无吸附能项目 — 去「吸附能项目」新建一组</div>';
      return;
    }
    const portfolioState = responseState === 'degraded' ? 'degraded' : 'ready';
    box.innerHTML = portfolioStateMarkup(portfolioState, rows, err, metadata) + rows.map(p => {
      const stages = p.stages || ['generate', 'submit', 'monitor', 'recover', 'analysis', 'report_done'];
      const steps = stages.map((st, i) => {
        let cls = '';
        if (i < p.stage_index) cls = 'done';
        else if (i === p.stage_index) cls = 'cur';
        let lbl = STAGE_LABEL[st] || st;
        if (st === 'recover' && p.recover_round) lbl += ` ${p.recover_round}/3`;
        return `<div class="pl-step ${cls}"><span class="dot"></span><span class="lbl">${VCS.esc(lbl)}</span></div>`;
      }).join('');
      const nextPage = p.stage_index <= 0 ? 'generate'
        : (p.stage_index <= 3 ? 'jobs' : 'project');
      const reportState = pipelineReportStates(p);
      return '<div class="pl-proj">' +
        '<div class="pl-head">' +
        (p.needs_human ? '<span class="redflag" title="需人工介入"></span>' : '') +
        VCS.elementBadge(p.name) +
        `<span class="nm">${VCS.esc(p.name || '(未命名)')}</span>` +
        (p.profile ? `<span class="pl-cluster">${VCS.esc(p.profile)}</span>` : '') +
        `<span class="plcount">${p.done}/${p.total} DONE</span>` +
        `<button class="btn quiet" data-goto="${nextPage}">继续下一步</button></div>` +
        `<div class="pl-steps">${steps}</div>${reportState}</div>`;
    }).join('');
  }

  // ── 项目管线卡片下:批次(campaign)三态小条形 + 机时 ──────────────────────
  // campaign 模块不可用 / 无批次 → 区块隐藏;任何异常都降级不拖垮仪表盘。
  function renderCampaigns(res) {
    const wrap = document.getElementById('db-campaigns');
    const box = document.getElementById('db-campaign-list');
    if (!wrap || !box) return;
    const camps = (res && res.available && res.campaigns) || [];
    if (!camps.length) { wrap.hidden = true; box.innerHTML = ''; return; }
    wrap.hidden = false;
    box.innerHTML = camps.map(c => {
      const t = Math.max(c.n_tasks || 0, 1);
      const s = c.states || {};
      const pct = n => Math.round((n || 0) / t * 100);
      const bud = c.budget || {};
      const cap = (bud.cap == null) ? '不限' : bud.cap;
      const mh = tr('dashboard.campaign.core_hours', '机时 {estimated} / {cap}', {
        estimated: bud.estimated == null ? '—' : bud.estimated, cap,
      });
      const summary = tr('dashboard.campaign.summary',
        '共 {total} 任务：完成 {completed} / 验证 {validated} / 采纳 {accepted}', {
          total: c.n_tasks, completed: s.completed || 0,
          validated: s.validated || 0, accepted: s.accepted || 0,
        });
      const meta = tr('dashboard.campaign.meta', '{states} · {total} 任务 · {hours}', {
        states: `${s.completed || 0}/${s.validated || 0}/${s.accepted || 0}`,
        total: c.n_tasks, hours: mh,
      });
      return '<div class="cmp-row">' +
        `<span class="cmp-name" title="${VCS.esc(c.name || '')}">${VCS.esc(c.name || '(未命名)')}</span>` +
        `<span class="cmp-bar" title="${VCS.esc(summary)}">` +
        `<i class="b-completed" style="width:${pct(s.completed)}%"></i>` +
        `<i class="b-validated" style="width:${pct(s.validated)}%"></i>` +
        `<i class="b-accepted" style="width:${pct(s.accepted)}%"></i></span>` +
        `<span class="cmp-meta">${VCS.esc(meta)}</span></div>`;
    }).join('');
  }

  // ── 卡片 4:待办(失效条目 / 需处理作业 / 未配置集群,各给一键跳转) ────────
  function renderTodo(jobs, stale, profiles) {
    const box = $('db-todo');
    if (!box) return;
    const items = [];
    const need = count(jobs, NEED_STATES);
    if (need) {
      items.push([tr('dashboard.todo.jobs',
        '{count} 个作业需处理（失败 / 未收敛 / 需人工）', { count: need }), 'jobs', '去作业页']);
    }
    if (stale.length) {
      items.push([tr('dashboard.todo.stale',
        '{count} 个失效台账条目待清理（目录或 job.yaml 已不存在）', { count: stale.length }),
        'jobs', '去清理']);
    }
    if (!profiles.length) {
      items.push(['尚未配置集群,无法提交作业', 'cluster', '去配置']);
    }
    if (!items.length) {
      box.innerHTML = '<div class="db-empty">暂无待办,一切正常</div>';
      return;
    }
    box.innerHTML = items.map(([txt, page, act]) =>
      `<div class="db-row"><span class="db-todo-txt">${VCS.esc(txt)}</span>` +
      `<span class="sp"></span>` +
      `<button class="btn quiet" data-goto="${VCS.esc(page)}">${VCS.esc(act)}</button>` +
      '</div>').join('');
  }

  function resumeScopeLabel(scope) {
    const labels = {
      'settings-llm': tr('settings.draft.llm', 'LLM 配置'),
      'settings-llm-key': tr('settings.draft.llm_key', '尚未保存的 API 密钥'),
      'settings-prompt': tr('settings.draft.prompt', '报告分析提示词'),
      'settings-paths': tr('settings.draft.paths', '数据路径'),
      'settings-autopilot': tr('settings.draft.autopilot', '自动托管设置'),
      'settings-figures': tr('settings.draft.figures', '出图偏好'),
      'project-import': tr('dashboard.resume.project_import', '项目导入流程草稿'),
      'draft-project-import': tr('dashboard.resume.project_import', '项目导入流程草稿'),
      'cluster-profile': tr('dashboard.resume.cluster_profile', '集群配置草稿'),
      'draft-cluster-profile': tr('dashboard.resume.cluster_profile', '集群配置草稿'),
      'research-notebook': tr('dashboard.resume.research_notebook', 'Research Notebook 待续录引用'),
    };
    return labels[scope] || String(scope || tr('dashboard.resume.unnamed', '未命名草稿'));
  }

  function resumeRoute(id, ref, workspace) {
    if (id === PROJECT_IMPORT_DRAFT_ID) {
      return typeof workspace.routeHash === 'function'
        ? workspace.routeHash('project-overview', { projectId: 'current' })
        : '#/projects/current/overview';
    }
    if (id === CLUSTER_PROFILE_DRAFT_ID) {
      return typeof workspace.routeHash === 'function'
        ? workspace.routeHash('environment-cluster') : '#/environment/cluster';
    }
    return String(ref && ref.route || '');
  }

  function runtimeResumeRoute(scope, workspace) {
    const id = String(scope || '').replace(/^draft-/, '');
    if (id === PROJECT_IMPORT_DRAFT_ID) return resumeRoute(id, {}, workspace);
    if (id === CLUSTER_PROFILE_DRAFT_ID) return resumeRoute(id, {}, workspace);
    return '#/environment/settings';
  }

  function localDraftAvailable(id, workspace) {
    const drafts = workspace && workspace.drafts;
    if (!(drafts && typeof drafts.load === 'function')) return false;
    try {
      const record = drafts.load(id);
      return !!(record && record.schema === 'vcstudio.unverified-draft/v1');
    } catch (_) { return false; }
  }

  function resumeRouteAvailable(route, projectId, workspace) {
    const parsed = workspace && typeof workspace.parseRoute === 'function'
      ? workspace.parseRoute(route) : null;
    if (!parsed) return false;
    if (typeof VCS.canActivatePage === 'function' && !VCS.canActivatePage(parsed.def.page)) {
      return false;
    }
    if (projectId && Array.isArray(workspace.projects) &&
      !workspace.projects.some(item => item.id === projectId)) return false;
    return true;
  }

  function collectResumeRows() {
    const workspace = VCS.workspace || {};
    const state = workspace.state || {};
    const refs = state.draft_refs && typeof state.draft_refs === 'object' ? state.draft_refs : {};
    const persistedIds = new Set(Object.keys(refs).map(id => 'draft-' + id));
    const runtimeScopes = VCS.unsaved && typeof VCS.unsaved.scopes === 'function'
      ? VCS.unsaved.scopes().filter(scope => !persistedIds.has(scope)) : [];
    const rows = runtimeScopes.map(scope => {
      const route = runtimeResumeRoute(scope, workspace);
      return {
        id: '', label: resumeScopeLabel(scope), route,
        projectId: '', updated: '', status: 'current',
        available: resumeRouteAvailable(route, '', workspace),
      };
    });
    Object.entries(refs).forEach(([id, ref]) => {
      if (!ref || ref.dirty !== true) return;
      const route = resumeRoute(id, ref, workspace);
      const reportDraft = /^report-/.test(id) || route.includes('/publish/');
      const notebookDraft = /^research-notebook-/.test(id);
      const specialDraft = id === PROJECT_IMPORT_DRAFT_ID || id === CLUSTER_PROFILE_DRAFT_ID;
      const projectId = specialDraft ? '' : String(ref.project_id || '');
      const available = localDraftAvailable(id, workspace) &&
        resumeRouteAvailable(route, projectId, workspace);
      rows.push({
        id,
        label: id === PROJECT_IMPORT_DRAFT_ID
          ? tr('dashboard.resume.project_import', '项目导入流程草稿')
          : id === CLUSTER_PROFILE_DRAFT_ID
            ? tr('dashboard.resume.cluster_profile', '集群配置草稿')
            : notebookDraft
              ? tr('dashboard.resume.research_notebook', 'Research Notebook 待续录引用')
            : reportDraft
              ? tr('dashboard.resume.report_draft', '报告配置草稿')
              : tr('dashboard.resume.workflow_draft', '工作流草稿'),
        route, projectId, updated: Number(ref.updated_at_ms || 0),
        status: available ? 'current' : 'unavailable', available,
      });
    });
    return rows;
  }

  function renderResumeCenter() {
    const box = $('db-resume'); if (!box) return;
    const workspace = VCS.workspace || {};
    const rows = collectResumeRows();
    if (!rows.length) {
      box.innerHTML = `<div class="db-empty">${VCS.esc(tr(
        'dashboard.resume.empty', '没有待恢复内容；显式保存的设置和已发布报告均已落盘。'))}</div>`;
      return;
    }
    box.innerHTML = rows.sort((a, b) => Number(b.updated) - Number(a.updated)).map(row => {
      const project = row.projectId && workspace.projects
        ? workspace.projects.find(item => item.id === row.projectId) : null;
      const status = row.status === 'current'
        ? tr('dashboard.resume.current', '当前设备可继续')
        : tr('dashboard.resume.unavailable', '当前设备不可用');
      const context = [project && project.name, row.updated
        ? new Date(row.updated).toLocaleString() : '', status].filter(Boolean).join(' · ');
      const action = row.available
        ? `data-resume-route="${VCS.esc(row.route)}" data-resume-project="${VCS.esc(row.projectId)}"`
        : `disabled aria-disabled="true" title="${VCS.esc(tr('dashboard.resume.local_only',
          '草稿正文只保存在创建它的设备；请回到原设备继续。'))}"`;
      return '<div class="db-row db-resume-row">' +
        `<span><b>${VCS.esc(row.label)}</b><small>${VCS.esc(context || tr(
          'dashboard.resume.current_session', '当前会话'))}</small></span>` +
        '<span class="sp"></span>' +
        `<button class="btn quiet" ${action}>${VCS.esc(row.available
          ? tr('dashboard.resume.open', '继续处理')
          : tr('dashboard.resume.not_here', '仅原设备可继续'))}</button></div>`;
    }).join('');
  }

  // 取数出错 → 页顶错误条(不再吞 error 假装 0 作业)
  function renderError(msgs) {
    const bar = $('db-error');
    if (!bar) return;
    if (!msgs.length) { bar.hidden = true; bar.textContent = ''; return; }
    bar.hidden = false;
    bar.textContent = tr('dashboard.data.partial_failure',
      '部分数据读取失败（下列读数可能不完整）：{errors}', { errors: msgs.join('；') });
  }

  // ── 取数 + 全量渲染(进页 / 启动时) ───────────────────────────────────────
  async function refresh() {
    renderPipeline(null, null, 'loading');
    const [jr, pr, cr, sr, campr, runtime] = await Promise.all([
      VCS.call('list_jobs'), VCS.call('proj_list'),
      VCS.call('list_profiles'), VCS.call('pipeline_status'),
      VCS.call('campaign_list'), VCS.call('pipeline_runtime_status')]);
    const jobs = (jr && jr.jobs) || [];
    const stale = (jr && jr.stale) || [];
    const projects = (pr && pr.projects) || [];
    const profiles = (cr && cr.profiles) || [];
    // 任一取数带 error → 错误条如实呈现,绝不静默当 0
    const errs = [];
    if (jr && jr.error) errs.push(tr('dashboard.error.jobs', '作业台账：{error}', { error: jr.error }));
    if (pr && pr.error) errs.push(tr('dashboard.error.projects', '项目列表：{error}', { error: pr.error }));
    if (cr && cr.error) errs.push(tr('dashboard.error.clusters', '集群配置：{error}', { error: cr.error }));
    const pipelineError = sr && sr.error
      ? sr.error : (!sr ? 'Pipeline status response is unavailable.' : null);
    if (pipelineError) errs.push(tr('dashboard.error.pipeline', '项目管线：{error}', { error: pipelineError }));
    if (runtime && runtime.error) errs.push(tr('dashboard.error.automation', '自动托管：{error}', { error: runtime.error }));
    renderError(errs);
    renderNums(jobs);
    renderRecent(jobs);
    renderTodo(jobs, stale, profiles);
    const pipelineState = sr && sr.status
      ? sr.status : (pipelineError ? 'unavailable' : 'ready');
    renderPipeline((sr && sr.projects) || [], pipelineError, pipelineState, sr || {});
    renderAutomation(runtime && runtime.state, runtime && runtime.error);
    renderCampaigns(campr);
    renderResumeCenter();
    if (VCS.pipeline && typeof VCS.pipeline.renderFeed === 'function') VCS.pipeline.renderFeed();
    if (typeof VCS.refreshNavFoot === 'function') VCS.refreshNavFoot();
    const el = $('db-stats');
    if (el) {
      el.textContent = tr('dashboard.stats.summary',
        '{jobs} 作业 · {projects} 吸附能项目 · {clusters} 集群', {
          jobs: jobs.length, projects: projects.length, clusters: profiles.length,
        });
    }
  }

  // ── 初始化:快捷入口 + data-goto 委托 ─────────────────────────────────────
  function init() {
    const page = $('page-dashboard');
    if (!page) return;
    document.addEventListener('vcs:pipeline-runtime', event => {
      renderAutomation(event && event.detail, null);
    });
    // 快捷入口三个大按钮
    const wire = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    renderStartActions(VCS.scenario);
    const actions = $('db-start-actions');
    if (actions) actions.addEventListener('click', e => {
      const b = e.target.closest('[data-action]');
      const a = b && ACTIONS[b.dataset.action];
      if (a) a[2]();
    });
    wire('db-go-generate', () => navTo('generate', { source: 'dashboard-quick-generate' }));
    wire('db-go-project', () => navTo('project', { source: 'dashboard-quick-project' }));
    wire('db-go-queue', async () => {
      const out = await navTo('jobs', { source: 'dashboard-quick-queue' });
      if (!out || !out.ok) return;
      const q = document.getElementById('jb-queue');
      if (q) q.click();          // 直接拉起集群队列(未配置集群时任务页会给提示)
    });
    // 大数字 / 最近活动行 / 待办按钮:统一 data-goto 跳页
    page.addEventListener('click', e => {
      const resume = e.target.closest('[data-resume-route]');
      if (resume && VCS.workspace) {
        const parsed = VCS.workspace.parseRoute(resume.dataset.resumeRoute);
        if (parsed) VCS.workspace.navigateRoute(parsed.id, {
          projectId: resume.dataset.resumeProject || undefined,
          query: parsed.query, source: 'resume-center',
        });
        return;
      }
      const portfolio = e.target.closest('[data-portfolio-route]');
      if (portfolio) {
        navigatePortfolio(portfolio.dataset.portfolioRoute,
          portfolio.dataset.portfolioFilter || '');
        return;
      }
      const el = e.target.closest('[data-goto]');
      if (el) navTo(el.dataset.goto);
    });
    document.addEventListener('vcs:unsaved', renderResumeCenter);
    page.addEventListener('keydown', e => {
      const el = e.target.closest('[data-goto][role="link"]');
      if (!el || e.key !== 'Enter') return;
      e.preventDefault();
      navTo(el.dataset.goto);
    });
    refresh();
  }

  // 切回仪表盘时刷新读数
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'dashboard') refresh();
  });
  document.addEventListener('vcs:scenario', e => renderStartActions(e.detail && e.detail.scenario));
  document.addEventListener('vcs:engine', () => renderStartActions(VCS.scenario));
  document.addEventListener('vcs:calculation', () => renderStartActions(VCS.scenario));
  document.addEventListener('vcs:language', () => {
    renderStartActions(VCS.scenario);
    refresh();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Dashboard = { refresh };
  if (window.__VCS_TEST__) {
    window.Dashboard.__test = {
      summarizeProjectPortfolio, portfolioStateMarkup, renderPipeline, navigatePortfolio,
      collectResumeRows, renderResumeCenter, resumeScopeLabel,
    };
  }
})();
