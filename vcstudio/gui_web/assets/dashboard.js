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

  // ── 卡片 1:状态汇总大数字(点击跳任务页) ─────────────────────────────────
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
      `<div class="db-num ${cls}" data-goto="jobs" title="点击查看任务页">` +
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
      '<div class="db-row" data-goto="jobs" title="点击查看任务页">' +
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
      return '<div class="pl-proj">' +
        '<div class="pl-head">' +
        (p.needs_human ? '<span class="redflag" title="需人工介入"></span>' : '') +
        VCS.elementBadge(p.name) +
        `<span class="nm">${VCS.esc(p.name || '(未命名)')}</span>` +
        `<span class="plcount">${p.done}/${p.total} DONE</span></div>` +
        `<div class="pl-steps">${steps}</div></div>`;
    }).join('');
  }

  // ── 卡片 4:待办(失效条目 / 需处理作业 / 未配置集群,各给一键跳转) ────────
  function renderTodo(jobs, stale, profiles) {
    const box = $('db-todo');
    if (!box) return;
    const items = [];
    const need = count(jobs, NEED_STATES);
    if (need) {
      items.push([`${need} 个作业需处理(失败 / 未收敛 / 需人工)`, 'jobs', '去任务页']);
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
    const [jr, pr, cr, sr] = await Promise.all([
      VCS.call('list_jobs'), VCS.call('proj_list'),
      VCS.call('list_profiles'), VCS.call('pipeline_status')]);
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

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Dashboard = { refresh };
})();
