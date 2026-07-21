// app.js — 桥 + 路由 + 通用组件。零依赖,零 CDN。
// Task 5/6 的页面模块只依赖此文件暴露的全局 VCS.*。
'use strict';

const VCS = {
  // pywebview 就绪信号:桥挂载完成后触发 'pywebviewready'
  ready: new Promise(res => {
    if (window.pywebview && window.pywebview.api) { res(); return; }
    window.addEventListener('pywebviewready', () => res(), { once: true });
  }),

  // ── 统一桥入口:等桥就绪 → 调用 → 透传返回 dict ──
  // NEED_PASSWORD 约定:后端返回 {error:'NEED_PASSWORD'} 时,弹密码框,
  // 由本入口回传 {needPassword:true, password} 给调用方,调用方带密码重试。
  async call(method, ...args) {
    await VCS.ready;
    const fn = window.pywebview && window.pywebview.api && window.pywebview.api[method];
    if (typeof fn !== 'function') {
      return { error: `桥方法不存在:${method}` };
    }
    const out = await fn(...args);
    if (out && out.error === 'NEED_PASSWORD') {
      const pw = await VCS.password();
      if (pw === null) return { cancelled: true };
      return { needPassword: true, password: pw };
    }
    return out;
  },

  // 弹密码框,返回 Promise<string|null>(取消为 null)
  password() {
    return new Promise(res => {
      let done = false;
      const finish = v => { if (!done) { done = true; res(v); } };
      const m = VCS.modal({
        title: '集群密码',
        bodyHTML:
          '<input id="pw" type="password" class="ipt" ' +
          'placeholder="输入密码(成功后存入系统凭据库)" autocomplete="off">',
        actions: [
          { label: '取消', quiet: true, onClick: mm => { mm.close(); finish(null); } },
          { label: '连接', primary: true, onClick: mm => {
              const v = mm.el.querySelector('#pw').value;
              mm.close(); finish(v);
            } },
        ],
      });
      // 便利:自动聚焦 + 回车提交
      const inp = m.el.querySelector('#pw');
      if (inp) {
        inp.focus();
        inp.addEventListener('keydown', e => {
          if (e.key === 'Enter') { const v = inp.value; m.close(); finish(v); }
        });
      }
      m.onDismiss = () => finish(null);
    });
  },

  // ── 通用模态:建 .modal-mask+.modal,返回 {el, close, onDismiss?} ──
  // actions: [{label, primary?, quiet?, onClick(modal)}]
  modal({ title, bodyHTML = '', body = null, actions = [] }) {
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    const card = document.createElement('div');
    card.className = 'modal';
    card.innerHTML =
      (title ? `<div class="m-title"></div>` : '') +
      `<div class="m-body"></div>` +
      `<div class="m-actions"></div>`;
    mask.appendChild(card);

    if (title) card.querySelector('.m-title').textContent = title;
    const bodyEl = card.querySelector('.m-body');
    if (body instanceof Node) bodyEl.appendChild(body);
    else bodyEl.innerHTML = bodyHTML;

    const handle = { el: card, mask, onDismiss: null };
    handle.close = () => { if (mask.parentNode) mask.parentNode.removeChild(mask); };

    const actEl = card.querySelector('.m-actions');
    (actions.length ? actions : [{ label: '关闭', quiet: true, onClick: h => h.close() }])
      .forEach(a => {
        const b = document.createElement('button');
        b.className = 'btn' + (a.primary ? ' primary' : a.quiet ? ' quiet' : '');
        b.textContent = a.label;
        b.addEventListener('click', () => a.onClick ? a.onClick(handle) : handle.close());
        actEl.appendChild(b);
      });

    // 点遮罩空白处 = 取消(触发 onDismiss 后关闭)
    mask.addEventListener('mousedown', e => {
      if (e.target === mask) { if (handle.onDismiss) handle.onDismiss(); handle.close(); }
    });
    // Esc 关闭
    const onKey = e => {
      if (e.key === 'Escape') {
        document.removeEventListener('keydown', onKey);
        if (handle.onDismiss) handle.onDismiss();
        handle.close();
      }
    };
    document.addEventListener('keydown', onKey);
    const origClose = handle.close;
    handle.close = () => { document.removeEventListener('keydown', onKey); origClose(); };

    document.body.appendChild(mask);
    return handle;
  },

  // 确认框,Promise<boolean>
  confirm(msg) {
    return new Promise(res => {
      let done = false;
      const finish = v => { if (!done) { done = true; res(v); } };
      const m = VCS.modal({
        title: '请确认',
        bodyHTML: `<div></div>`,
        actions: [
          { label: '取消', quiet: true, onClick: mm => { mm.close(); finish(false); } },
          { label: '确定', primary: true, onClick: mm => { mm.close(); finish(true); } },
        ],
      });
      m.el.querySelector('.m-body div').textContent = String(msg);
      m.onDismiss = () => finish(false);
    });
  },

  // 未知 SSH 主机确认：只把后端实际捕获的 host + SHA256 指纹回传为精确 pin。
  // 缺少任何关键证据时不提供“信任”路径；跳板机和目标机可依次调用本流程。
  async confirmHostKey(result) {
    const host = String(result && result.host || '').trim();
    const fingerprint = String(result && result.fingerprint || '').trim();
    const algorithm = String(result && result.algorithm || '').trim();
    if (!host || !fingerprint || !fingerprint.startsWith('SHA256:')) {
      VCS.log('服务器没有返回可核对的主机名和 SHA256 指纹，已阻止连接。请先在“集群”页测试连接。', 'failc');
      return null;
    }
    const ok = await VCS.confirm(
      '首次连接服务器，请与管理员提供的信息逐字核对：\n\n' +
      '主机：' + host + '\n' +
      '算法：' + (algorithm || '未提供') + '\n' +
      'SHA256 指纹：' + fingerprint + '\n\n' +
      '只有完全一致时才选择“确定”。');
    return ok ? { host, fingerprint, algorithm } : null;
  },

  // 短提示条,2.6s 自动消失。kind: '' | 'ok' | 'fail'
  toast(msg, kind = '') {
    const t = document.createElement('div');
    t.className = 'toast' + (kind ? ' ' + kind : '');
    t.textContent = String(msg);
    document.body.appendChild(t);
    setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 2600);
  },

  // 状态 → pill HTML(类名同 mockup)
  pill(state) {
    const M = {
      RUNNING: ['run', 'RUN'], QUEUED: ['q', 'QUEUE'], SUBMITTED: ['q', 'SUBMIT'],
      UPLOADED: ['q', 'UPLOAD'], DONE: ['ok', 'DONE'], FAILED: ['fail', 'FAIL'],
      UNCONVERGED: ['fail', '未收敛'], NEEDS_HUMAN: ['warn', '需人工'], CREATED: ['q', '草稿'],
    };
    const [cls, txt] = M[state] || ['q', state];
    return `<span class="pill ${cls}"><i></i>${VCS.esc(txt)}</span>`;
  },

  // 顶插日志到 .log 容器(带 HH:MM:SS 时间戳),保留 200 行。
  // 首次调用时若无容器则建一个挂到当前可见页尾。cls: '' | 'okc' | 'failc'
  log(line, cls = '') {
    // 优先用当前可见页自带的日志锚点(如作业页的 #jobs-log / .log[data-anchor]),
    // 否则退回文档首个 .log,再没有才自动新建 —— 向后兼容 Task 4 行为。
    const visible = document.querySelector('main section[data-page]:not([hidden])');
    let box = (visible && visible.querySelector('.log[data-anchor], .log')) ||
              document.querySelector('.log');
    if (!box) {
      box = document.createElement('div');
      box.className = 'log';
      box.innerHTML = '<div class="head">操作日志</div>';
      const page = document.querySelector('main section[data-page]:not([hidden])') || document.querySelector('main');
      page.appendChild(box);
    }
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, '0');
    const mm = String(now.getMinutes()).padStart(2, '0');
    const ss = String(now.getSeconds()).padStart(2, '0');
    const row = document.createElement('div');
    row.innerHTML = `<span class="t">${hh}:${mm}:${ss}</span>` +
      (cls ? `<span class="${cls}">${VCS.esc(line)}</span>` : VCS.esc(line));
    const head = box.querySelector('.head');
    box.insertBefore(row, head ? head.nextSibling : box.firstChild);
    // 保留 200 行(不含 .head)
    const rows = box.querySelectorAll('div:not(.head)');
    for (let i = rows.length - 1; i >= 200; i--) rows[i].remove();
  },

  // HTML 转义,防注入
  esc(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  },
};

window.VCS = VCS;

// ── 统一页面导航:手动点侧栏与程序化“下一步”共用同一路由 ──
function activatePage(page, sourceLink, detail) {
  const name = String(page || '');
  const section = Array.from(document.querySelectorAll('main section[data-page]'))
    .find(s => s.dataset.page === name);
  const link = sourceLink || Array.from(document.querySelectorAll('nav a[data-page]'))
    .find(a => a.dataset.page === name);
  if (!name || !section || !link) return false;
  // 工作模式是访问闸，不只是视觉隐藏。程序化导航也不能绕过侧栏裁剪。
  if (link.hasAttribute('data-scene-hidden') || link.hidden) {
    VCS.toast('当前工作模式不需要此页面；可在“设置 → 本次计算”切换模式', 'fail');
    return false;
  }
  document.querySelectorAll('nav a').forEach(x => x.classList.toggle('on', x === link));
  document.querySelectorAll('main section[data-page]').forEach(
    s => { s.hidden = s.dataset.page !== name; });
  document.dispatchEvent(new CustomEvent('vcs:page', {
    detail: Object.assign({}, detail || {}, { page: name }),
  }));
  return true;
}

document.addEventListener('click', e => {
  const a = e.target.closest('a[data-page]');
  if (!a) return;
  e.preventDefault();
  activatePage(a.dataset.page, a);
});

function findJobRow(jobDir) {
  const comparablePath = value => {
    let out = String(value || '').replace(/\\/g, '/').replace(/\/+$/, '');
    if (/^[A-Za-z]:\//.test(out)) out = out.toLowerCase();
    return out;
  };
  const wanted = comparablePath(jobDir);
  return Array.from(document.querySelectorAll('#jobs-card tr[data-dir]'))
    .find(row => comparablePath(row.dataset.dir) === wanted) || null;
}

// 跳作业页后清除会遮住新作业的筛选,展开所在组,选中并滚动到该行。
// 不依赖 jobs.js 内部 State,只通过它已有的 DOM 事件契约交互。
async function focusPendingJob(jobDir) {
  if (!jobDir) return false;
  if (window.Jobs && typeof window.Jobs.reload === 'function') {
    await window.Jobs.reload();
  }
  ['jf-cluster', 'jf-status'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.value) {
      el.value = '';
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
  let row = findJobRow(jobDir);
  if (row && row.hidden && row.dataset.grp) {
    const group = Array.from(document.querySelectorAll('#jobs-card tr.grp-head'))
      .find(head => head.dataset.grp === row.dataset.grp);
    if (group) group.click();
    row = findJobRow(jobDir);                 // 展开会重绘 table,需重取节点
  }
  if (!row || row.hidden) return false;
  if (!row.classList.contains('sel')) row.click();
  if (typeof row.scrollIntoView === 'function') {
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  const focusTarget = row.querySelector('.jrow-chk') || row;
  if (focusTarget === row) row.tabIndex = -1;
  if (typeof focusTarget.focus === 'function') {
    try { focusTarget.focus({ preventScroll: true }); } catch (_) { focusTarget.focus(); }
  }
  return true;
}

// 程序化导航公开入口。options.focusJobDir 专用于“生成 → 提交”的待提交作业聚焦。
VCS.navigate = async function (page, options = {}) {
  const ok = activatePage(page, null, { source: options.source || 'programmatic' });
  if (!ok) return { ok: false, focused: false };
  let focused = true;
  if (options.focusJobDir) focused = await focusPendingJob(options.focusJobDir);
  else if (options.focusSelector) {
    const el = document.querySelector(options.focusSelector);
    focused = !!el;
    if (el) {
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' });
      if (typeof el.focus === 'function') el.focus();
    }
  }
  return { ok: true, focused };
};

// 统一“下一步”弹窗:文案均以 textContent 写入;主按钮可跳页并携带聚焦上下文。
VCS.nextStep = function ({ title = '操作已完成', message = '', detail = '',
  primaryLabel = '前往下一步', stayLabel = '留在本页', page = '', focusJobDir = '',
  focusSelector = '', onPrimary = null } = {}) {
  const body = document.createElement('div');
  const msg = document.createElement('p');
  msg.textContent = String(message || '');
  body.appendChild(msg);
  if (detail) {
    const more = document.createElement('div');
    more.className = 'sub';
    more.textContent = String(detail);
    body.appendChild(more);
  }
  return VCS.modal({
    title,
    body,
    actions: [
      { label: stayLabel, quiet: true, onClick: modal => modal.close() },
      { label: primaryLabel, primary: true, onClick: async modal => {
          modal.close();
          try {
            if (typeof onPrimary === 'function') await onPrimary();
            else if (page) {
              const out = await VCS.navigate(page, { focusJobDir, focusSelector,
                source: 'next-step' });
              if (out.ok && focusJobDir && !out.focused) {
                VCS.toast('已进入任务页，请在列表中选择新作业');
              }
            }
          } catch (err) {
            VCS.toast('无法打开下一步:' + (err && err.message ? err.message : err), 'fail');
          }
        } },
    ],
  });
};

// ── 主题:三选可切换(经典深邃 / 学术浅色 / 深空监控) ──
// 启动时 <head> 内联脚本已按 localStorage 先粉刷防闪烁;此处提供切换 + 校准入口。
const THEMES = ['classic', 'paper', 'deep'];
VCS.themeApply = function (name) {
  const t = THEMES.indexOf(name) >= 0 ? name : 'classic';
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('vcs.theme', t); } catch (_) { /* 隐私模式忽略 */ }
  return t;
};

// ── 侧栏底部:动态显示当前默认集群(取 profiles 首个;无则"未配置集群") ──
VCS.refreshNavFoot = async function () {
  const foot = document.getElementById('nav-foot');
  if (!foot) return;
  try {
    const r = await VCS.call('list_profiles');
    const profs = (r && r.profiles) || [];
    if (!profs.length) { foot.textContent = '未配置集群'; return; }
    const p = profs[0];
    const sched = p.scheduler ? ' · ' + p.scheduler : '';
    foot.innerHTML = `${VCS.esc((p.username ? p.username + '@' : '') + (p.name || ''))}` +
      `<br>${VCS.esc((p.hostname || '') + sched)}`;
  } catch (_) { foot.textContent = '未配置集群'; }
};

// ── 化学元素徽章:从体系名提取首个金属符号 → CPK 风格色块 + 白字(色盲安全靠文字) ──
const EL_CPK = {
  Co: '#D45A7C', Fe: '#C85A22', Ni: '#2E7C39', Mo: '#2E8C8C', W: '#2A6BA6',
  Ti: '#6A7480', V: '#586A88', Mn: '#8C5AB4', Cu: '#B06A2C', Zn: '#5E7396',
  Pt: '#6E7B99', Pd: '#3B7C86', Ru: '#2E8F7E', Ir: '#4A6FA5', Ag: '#6E7C8C', Au: '#9C7A1E',
};
VCS.elementBadge = function (systemName) {
  const s = String(systemName || '');
  const re = /[A-Z][a-z]?/g;
  let m;
  while ((m = re.exec(s))) {
    const sym = m[0];
    if (EL_CPK[sym]) {
      return `<span class="elbadge" style="--el:${EL_CPK[sym]}" title="金属位:${sym}">${sym}</span>`;
    }
  }
  return '';   // 识别不出不加(不猜)
};

// ── 全局自动托管编排器:按间隔调 pipeline_tick,渲染事件 + 健康读数 + 断线横幅 ──
VCS.pipeline = {
  timer: null, running: false, failStreak: 0, lastSuccess: null,
  events: [],   // 最近 20 条(新在前),前端到达时间戳
};

function hhmm(ts) {
  const s = String(ts || '');
  const m = s.match(/(\d{2}):(\d{2})/);
  if (m) return m[1] + ':' + m[2];
  const d = new Date();
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function renderHealth() {
  const el = document.getElementById('conn');
  if (!el) return;
  const p = VCS.pipeline;
  if (p.failStreak >= 1) {
    el.className = 'warn';
    el.innerHTML = `<span class="dot g"></span>同步失败×${p.failStreak}` +
      `<span class="bridge">界面桥 ✓</span>`;
  } else if (p.lastSuccess) {
    el.className = '';
    el.innerHTML = `<span class="dot g"></span>同步 ${hhmm(p.lastSuccess)} ✓` +
      `<span class="bridge">界面桥 ✓</span>`;
  } else {
    el.className = '';
    el.innerHTML = `<span class="dot g"></span><span class="bridge">界面桥 ✓</span>`;
  }
}

function renderConnBanner() {
  const b = document.getElementById('conn-banner');
  if (!b) return;
  const p = VCS.pipeline;
  if (p.failStreak >= 2) {
    b.classList.add('show');
    b.innerHTML = `⚠ 集群连接可能已断:最近 <b>${p.failStreak}</b> 次同步失败` +
      `(上次成功 ${p.lastSuccess ? hhmm(p.lastSuccess) : '—'}),数据可能过期`;
  } else {
    b.classList.remove('show');
  }
  // 任务表数据过期视觉(降饱和 + 角标);表重渲后由下一拍再置
  document.querySelectorAll('#jobs-card tr[data-dir]').forEach(
    tr => tr.classList.toggle('stale', p.failStreak >= 2));
}

function renderFeed() {
  const box = document.getElementById('db-feed');
  if (!box) return;
  const evs = VCS.pipeline.events;
  if (!evs.length) {
    box.innerHTML = '<div class="db-empty">自动托管开启后，这里显示每轮监控、续算、下载和报告事件</div>';
    return;
  }
  const KL = { refresh: '同步', continue: '续算', fetch: '下载', report_done: '报告',
    skip: '跳过', error: '错误' };
  box.innerHTML = evs.map(e => {
    const kcls = e.kind === 'report_done' ? 'report'
      : (e.kind === 'error' ? 'err' : (e.kind === 'skip' ? 'skip' : ''));
    const label = KL[e.kind] || e.kind;
    const cluster = e.cluster
      ? `<span class="fcluster" title="服务器">${VCS.esc(e.cluster)}</span>` : '';
    const btn = (e.kind === 'report_done' && (e.report || e.figures_dir))
      ? `<button class="btn quiet fbtn" data-open="${VCS.esc(e.report || e.figures_dir)}">打开</button>` : '';
    return `<div class="feed-row"><span class="fk ${kcls}">${VCS.esc(label)}</span>${cluster}` +
      `<span class="ftxt" title="${VCS.esc(e.text || '')}">${VCS.esc(e.text || '')}</span>` +
      `<span class="ft">${VCS.esc(e.time || '')}</span>${btn}</div>`;
  }).join('');
}

function reportToast(ev) {
  const t = document.createElement('div');
  t.className = 'toast ok';
  t.textContent = '报告已自动生成:' + (ev.project || '');
  const b = document.createElement('button');
  b.className = 'btn quiet';
  b.style.marginLeft = '12px';
  b.textContent = '打开';
  b.addEventListener('click', () => {
    VCS.call('open_dir', ev.report || ev.figures_dir);
    if (t.parentNode) t.parentNode.removeChild(t);
  });
  t.appendChild(b);
  document.body.appendChild(t);
  setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 8000);
}

function onPipelineOutcome(out) {
  const p = VCS.pipeline;
  const now = hhmm(out.last_sync);
  const clusterFail = (out.errors || []).some(e => String(e).includes('同步失败'));
  if ((out.synced || 0) > 0) { p.failStreak = 0; p.lastSuccess = out.last_sync; }
  else if (clusterFail) { p.failStreak++; }
  // 事件累积(新在前,最多 20 条);report_done 醒目 toast,其余汇总 toast(避免刷屏)
  let other = 0;
  (out.events || []).forEach(e => {
    p.events.unshift(Object.assign({ time: now }, e));
    if (e.kind === 'report_done') reportToast(Object.assign({ time: now }, e));
    else if (e.kind !== 'skip') other++;
  });
  // 错误也进 feed(不用 VCS.log:避免给无日志区的页面凭空插入日志框)
  (out.errors || []).forEach(err => p.events.unshift({ kind: 'error', text: err, time: now }));
  p.events = p.events.slice(0, 20);
  if (other) VCS.toast('自动托管：本轮 ' + other + ' 条动态', '');
  if ((out.errors || []).length) VCS.toast('自动托管遇到 ' + out.errors.length + ' 个问题（见任务动态）', 'fail');
  renderHealth();
  renderConnBanner();
  renderFeed();
}

async function pipelineTick() {
  const p = VCS.pipeline;
  if (p.running) return;
  p.running = true;
  try {
    const out = await VCS.call('pipeline_tick');
    if (!out || out.error) {
      p.failStreak++;
      renderHealth(); renderConnBanner();
      return;
    }
    onPipelineOutcome(out);
  } finally {
    p.running = false;
  }
}
VCS.pipeline.tick = pipelineTick;
VCS.pipeline.renderFeed = renderFeed;

// 从 config 校准主题 + 按开关/间隔(重新)装载定时器;设置页保存后可再调本函数
VCS.pipeline.reconfigure = async function () {
  const s = await VCS.call('settings_get');
  const ui = (s && s.ui) || {};
  if (ui.theme) VCS.themeApply(ui.theme);
  // Fail closed when settings cannot be read.  The Li-S submit flow explicitly
  // enables automation for its managed project after a successful submission;
  // a fresh install must never start mutating an old ledger on its own.
  const enabled = ui.autopilot === true;
  const interval = (Number(ui.poll_interval) || 10) * 60000;
  if (VCS.pipeline.timer) { clearInterval(VCS.pipeline.timer); VCS.pipeline.timer = null; }
  if (enabled) {
    VCS.pipeline.timer = setInterval(pipelineTick, interval);
    setTimeout(pipelineTick, 3500);                   // 启动后先跑一拍,让用户见到动态
  }
};

// feed 内「打开」按钮委托 + 切回仪表盘时重渲 feed
document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('#db-feed [data-open]');
  if (b) { e.preventDefault(); VCS.call('open_dir', b.dataset.open); }
});
document.addEventListener('vcs:page', e => {
  if (e.detail && e.detail.page === 'dashboard') renderFeed();
});

// ── i18n:按 data-i18n 属性替换文本(缺键保留原中文,en 缺失回落 zh 已在后端处理) ──
VCS.i18n = { dict: {}, lang: 'zh' };
VCS.applyI18n = function (dict) {
  if (dict) VCS.i18n.dict = dict;
  const d = VCS.i18n.dict || {};
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.getAttribute('data-i18n');
    if (k && Object.prototype.hasOwnProperty.call(d, k)) el.textContent = d[k];
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const k = el.getAttribute('data-i18n-ph');
    if (k && Object.prototype.hasOwnProperty.call(d, k)) el.setAttribute('placeholder', d[k]);
  });
};
VCS.loadLang = async function (lang) {
  let lg = lang;
  if (!lg) { const g = await VCS.call('lang_get'); lg = (g && g.lang) || 'zh'; }
  VCS.i18n.lang = lg;
  const r = await VCS.call('i18n_dict', lg);
  if (r && r.dict) VCS.applyI18n(r.dict);
  try { document.documentElement.lang = (lg === 'en' ? 'en' : 'zh-CN'); } catch (_) { /* 忽略 */ }
  return lg;
};

// ── 工作模式:按 data-scene 点分路径显隐 nav 项与卡片(镜像 scenarios.is_visible 口径) ──
VCS.scenario = null;
VCS.activeCalculation = '';
function sceneVisible(sc, path) {
  if (!sc || !path) return true;
  const parts = path.split('.'); const head = parts[0]; const rest = parts.slice(1);
  if (head === 'pages') return rest.length > 0 && (sc.pages || []).indexOf(rest[0]) >= 0;
  if (head === 'engines') return rest.length > 0 && (sc.engines || []).indexOf(rest[0]) >= 0;
  if (head === 'reactions') return rest.length > 0 && (sc.reaction_presets || []).indexOf(rest[0]) >= 0;
  if (head === 'figures') return rest.length > 0 && (sc.figure_preset_order || []).indexOf(rest[0]) >= 0;
  if (head === 'cards') {
    let node = sc.cards || {};
    for (let i = 0; i < rest.length; i++) {
      const k = rest[i];
      if (typeof node !== 'object' || node === null || !(k in node)) return true;  // 未覆盖 → 默认可见
      node = node[k];
    }
    return (typeof node === 'boolean') ? node : true;
  }
  return true;                             // 未知路径族保守可见(fail-open)
}
VCS.sceneVisible = sceneVisible;
VCS.applyScenario = function (sc) {
  if (sc) VCS.scenario = sc;
  const s = VCS.scenario;
  if (!s) return;
  document.querySelectorAll('[data-scene]').forEach(el => {
    el.toggleAttribute('data-scene-hidden', !sceneVisible(s, el.getAttribute('data-scene')));
  });
  const chip = document.getElementById('mode-chip');
  if (chip) chip.textContent = '工作模式：' + (s.name || s.key || '未选择');
  // 当前页被模式隐藏 → 去该模式的默认入口；再失败才退回概览。
  const cur = document.querySelector('nav a.on');
  if (cur && cur.hasAttribute('data-scene-hidden')) {
    const landing = (s.defaults && s.defaults.landing_page) || 'dashboard';
    if (!activatePage(landing, null, { source: 'work-mode' })) {
      activatePage('dashboard', null, { source: 'work-mode-fallback' });
    }
  }
  document.dispatchEvent(new CustomEvent('vcs:scenario', { detail: { scenario: s } }));
};

VCS.applyCalculation = function (key) {
  VCS.activeCalculation = String(key || '');
  document.querySelectorAll('[data-task]').forEach(el => {
    const tokens = String(el.getAttribute('data-task') || '').split(/\s+/).filter(Boolean);
    el.toggleAttribute('data-task-hidden', !!VCS.activeCalculation &&
      tokens.indexOf(VCS.activeCalculation) < 0 && tokens.indexOf('all') < 0);
  });
  document.dispatchEvent(new CustomEvent('vcs:calculation', {
    detail: { activeCalculation: VCS.activeCalculation },
  }));
};

VCS.loadCalculation = async function () {
  const r = await VCS.call('calculation_get');
  if (r && r.ok) VCS.applyCalculation(r.active_calculation || '');
  return VCS.activeCalculation;
};

// ── 首启工作模式选择模态(config 无 ui.scenario 时;只给四个常用模式) ──
async function firstLaunchScenario() {
  const r = await VCS.call('scenario_list');
  const list = ((r && r.scenarios) || []).filter(s => s.primary);
  if (!list.length) return;
  const box = document.createElement('div');
  box.innerHTML = '<div class="scene-grid">' + list.map(s =>
    `<div class="scene-card" data-key="${VCS.esc(s.key)}"><b>${VCS.esc(s.name)}</b>` +
    `<span>${VCS.esc(s.description)}</span></div>`).join('') + '</div>';
  const m = VCS.modal({ title: '这次要做哪类计算？（之后可在设置中切换）',
    body: box, actions: [] });
  m.el.classList.add('modal-wide');
  box.querySelectorAll('.scene-card').forEach(c => c.addEventListener('click', async () => {
    const key = c.dataset.key;
    m.close();
    const res = await VCS.call('scenario_set', key);
    if (res && res.scenario) {
      VCS.applyScenario(res.scenario);
      await VCS.loadCalculation();
      VCS.toast('已选择工作模式：' + (res.scenario.name || key));
    }
  }));
}
VCS.firstLaunchScenario = firstLaunchScenario;

document.addEventListener('click', e => {
  const chip = e.target.closest && e.target.closest('#mode-chip');
  if (chip) { e.preventDefault(); VCS.navigate('settings', { source: 'mode-chip' }); }
});

// ── 桥就绪:界面桥读数(小字,避免误读为集群已连)+ 启动自动托管编排器 ──
VCS.ready.then(async () => {
  try {
    await window.pywebview.api.ping();     // 确认桥活性(失败则走 catch)
    renderHealth();                        // 初始:仅"界面桥 ✓"
    VCS.refreshNavFoot();
    VCS.pipeline.reconfigure();
    try { await VCS.loadLang(); } catch (_) { /* 语言失败不挡界面 */ }
    try {
      const sc = await VCS.call('scenario_get');
      if (sc && sc.scenario) {
        VCS.applyScenario(sc.scenario);
        await VCS.loadCalculation();
        if (!sc.configured) firstLaunchScenario();   // 首启弹场景选择模态
      }
    } catch (_) { /* 场景失败不挡界面 */ }
  } catch (_) {
    const el = document.getElementById('conn');
    if (el) el.innerHTML = '<span class="dot g"></span><span class="bridge">界面桥 响应异常</span>';
  }
});
