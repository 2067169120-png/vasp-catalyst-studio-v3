// dashboard.js — 仪表盘首页:作业状态汇总 / 最近活动 / 快捷入口 / 待办。
// 纯前端组合现有桥方法(list_jobs / proj_list / list_profiles),不新增后端调用面。
// 只依赖 app.js 暴露的 VCS.*;全部插值走 VCS.esc;零 emoji;中文文案。
'use strict';
(function () {
  const $ = id => document.getElementById(id);

  const QUEUE_STATES = ['QUEUED', 'SUBMITTED', 'UPLOADED'];
  const NEED_STATES = ['FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'];

  function count(jobs, states) {
    return jobs.filter(j => states.indexOf(j.state) >= 0).length;
  }

  // 跳页:点对应 nav a(与手动点导航完全同路径,触发 vcs:page 刷新)
  function navTo(page) {
    const a = document.querySelector('nav a[data-page=' + page + ']');
    if (a) a.click();
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

  function openInputsSubmit() {
    navTo('jobs');
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
    lis_new: ['Li-S 一站式：开始新的吸附计算', '参考能 + 固定 INCAR + slab / adsorption POSCAR → 自动托管整组任务', async () => {
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
        const verb = route && route.page === 'project' ? '打开结果工具' : '开始准备';
        const engine = String(VCS.activeEngine || 'vasp').toUpperCase();
        ACTIONS.selected_calculation = [
          `${verb}：${engine} · ${task.name_zh}`,
          `${task.requires || '按提示准备输入'} → ${task.outputs || '计算结果'}`,
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
    if (sub) sub.textContent = '当前工作模式：' + (sc.name || sc.key) +
      '。这里只保留本次需要的入口，可随时在设置中切换。';
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
    box.innerHTML = items.map(([cls, label, n]) =>
      `<div class="db-num ${cls}" data-goto="jobs" title="点击查看作业页">` +
      `<b>${n}</b><span>${VCS.esc(label)}</span></div>`).join('');
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
    box.innerHTML = rows.map(r =>
      '<div class="db-row" data-goto="jobs" title="点击查看作业页">' +
      VCS.elementBadge(r.name) +
      `<span class="name">${VCS.esc(r.name)}</span>` +
      (r.project ? `<span class="db-proj">${VCS.esc(r.project)}</span>` : '') +
      '<span class="sp"></span>' + VCS.pill(r.state) +
      `<span class="db-time">${VCS.esc(r.updated || '')}</span></div>`).join('');
  }

  // ── 卡片:项目管线(每项目一行站点进度;当前站高亮,NEEDS_HUMAN 红点) ─────────
  const STAGE_LABEL = { generate: '生成', submit: '提交', monitor: '监控',
    recover: '恢复', analysis: '分析', report_done: '报告' };

  function renderPipeline(projects, err) {
    const box = $('db-pipeline');
    if (!box) return;
    if (err) { box.innerHTML = `<div class="pl-empty">读取项目管线失败:${VCS.esc(err)}</div>`; return; }
    if (!projects.length) {
      box.innerHTML = '<div class="pl-empty">暂无吸附能项目 — 去「吸附能项目」新建一组</div>';
      return;
    }
    box.innerHTML = projects.map(p => {
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
      return '<div class="pl-proj">' +
        '<div class="pl-head">' +
        (p.needs_human ? '<span class="redflag" title="需人工介入"></span>' : '') +
        VCS.elementBadge(p.name) +
        `<span class="nm">${VCS.esc(p.name || '(未命名)')}</span>` +
        (p.profile ? `<span class="pl-cluster">${VCS.esc(p.profile)}</span>` : '') +
        `<span class="plcount">${p.done}/${p.total} DONE</span>` +
        `<button class="btn quiet" data-goto="${nextPage}">继续下一步</button></div>` +
        `<div class="pl-steps">${steps}</div></div>`;
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
      const mh = '机时 ' + (bud.estimated == null ? '—' : bud.estimated) + ' / ' + cap;
      return '<div class="cmp-row">' +
        `<span class="cmp-name" title="${VCS.esc(c.name || '')}">${VCS.esc(c.name || '(未命名)')}</span>` +
        `<span class="cmp-bar" title="共 ${c.n_tasks} 任务:完成 ${s.completed || 0} / 验证 ${s.validated || 0} / 采纳 ${s.accepted || 0}">` +
        `<i class="b-completed" style="width:${pct(s.completed)}%"></i>` +
        `<i class="b-validated" style="width:${pct(s.validated)}%"></i>` +
        `<i class="b-accepted" style="width:${pct(s.accepted)}%"></i></span>` +
        `<span class="cmp-meta">${s.completed || 0}/${s.validated || 0}/${s.accepted || 0}` +
        ` · ${c.n_tasks} 任务 · ${VCS.esc(mh)}</span></div>`;
    }).join('');
  }

  // ── 卡片 4:待办(失效条目 / 需处理作业 / 未配置集群,各给一键跳转) ────────
  function renderTodo(jobs, stale, profiles) {
    const box = $('db-todo');
    if (!box) return;
    const items = [];
    const need = count(jobs, NEED_STATES);
    if (need) {
      items.push([`${need} 个作业需处理(失败 / 未收敛 / 需人工)`, 'jobs', '去作业页']);
    }
    if (stale.length) {
      items.push([`${stale.length} 个失效台账条目待清理(目录或 job.yaml 已不存在)`,
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

  // 取数出错 → 页顶错误条(不再吞 error 假装 0 作业)
  function renderError(msgs) {
    const bar = $('db-error');
    if (!bar) return;
    if (!msgs.length) { bar.hidden = true; bar.textContent = ''; return; }
    bar.hidden = false;
    bar.textContent = '部分数据读取失败(下列读数可能不完整):' + msgs.join(';');
  }

  // ── 取数 + 全量渲染(进页 / 启动时) ───────────────────────────────────────
  async function refresh() {
    const [jr, pr, cr, sr, campr] = await Promise.all([
      VCS.call('list_jobs'), VCS.call('proj_list'),
      VCS.call('list_profiles'), VCS.call('pipeline_status'),
      VCS.call('campaign_list')]);
    const jobs = (jr && jr.jobs) || [];
    const stale = (jr && jr.stale) || [];
    const projects = (pr && pr.projects) || [];
    const profiles = (cr && cr.profiles) || [];
    // 任一取数带 error → 错误条如实呈现,绝不静默当 0
    const errs = [];
    if (jr && jr.error) errs.push('作业台账:' + jr.error);
    if (pr && pr.error) errs.push('项目列表:' + pr.error);
    if (cr && cr.error) errs.push('集群配置:' + cr.error);
    if (sr && sr.error) errs.push('项目管线:' + sr.error);
    renderError(errs);
    renderNums(jobs);
    renderRecent(jobs);
    renderTodo(jobs, stale, profiles);
    renderPipeline((sr && sr.projects) || [], sr && sr.error);
    renderCampaigns(campr);
    if (VCS.pipeline && typeof VCS.pipeline.renderFeed === 'function') VCS.pipeline.renderFeed();
    if (typeof VCS.refreshNavFoot === 'function') VCS.refreshNavFoot();
    const el = $('db-stats');
    if (el) {
      el.innerHTML = `<b>${jobs.length}</b> 作业 · <b>${projects.length}</b> 吸附能项目 · ` +
        `<b>${profiles.length}</b> 集群`;
    }
  }

  // ── 初始化:快捷入口 + data-goto 委托 ─────────────────────────────────────
  function init() {
    const page = $('page-dashboard');
    if (!page) return;
    // 快捷入口三个大按钮
    const wire = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    renderStartActions(VCS.scenario);
    const actions = $('db-start-actions');
    if (actions) actions.addEventListener('click', e => {
      const b = e.target.closest('[data-action]');
      const a = b && ACTIONS[b.dataset.action];
      if (a) a[2]();
    });
    wire('db-go-generate', () => navTo('generate'));
    wire('db-go-project', () => navTo('project'));
    wire('db-go-queue', () => {
      navTo('jobs');
      const q = document.getElementById('jb-queue');
      if (q) q.click();          // 直接拉起集群队列(未配置集群时任务页会给提示)
    });
    // 大数字 / 最近活动行 / 待办按钮:统一 data-goto 跳页
    page.addEventListener('click', e => {
      const el = e.target.closest('[data-goto]');
      if (el) navTo(el.dataset.goto);
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

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Dashboard = { refresh };
})();
