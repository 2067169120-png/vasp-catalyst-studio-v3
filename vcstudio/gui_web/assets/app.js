// app.js — 桥 + 路由 + 通用组件。零依赖,零 CDN。
// Task 5/6 的页面模块只依赖此文件暴露的全局 VCS.*。
'use strict';

const vcsModalStack = [];
const vcsModalBackground = new Map();

function vcsModalFocusable(container) {
  return Array.from(container.querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
    'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'))
    .filter(element => !element.closest('[hidden],[inert],[aria-hidden="true"],fieldset[disabled]'));
}

function vcsModalRestore(element, saved) {
  if (!element || !saved) return;
  if (saved.inert) element.setAttribute('inert', ''); else element.removeAttribute('inert');
  if (saved.ariaHidden == null) element.removeAttribute('aria-hidden');
  else element.setAttribute('aria-hidden', saved.ariaHidden);
}

function vcsModalSyncBackground() {
  const top = vcsModalStack.length ? vcsModalStack[vcsModalStack.length - 1] : null;
  if (!top) {
    vcsModalBackground.forEach((saved, element) => vcsModalRestore(element, saved));
    vcsModalBackground.clear(); return;
  }
  Array.from(document.body.children).forEach(element => {
    if (!vcsModalBackground.has(element)) {
      vcsModalBackground.set(element, {
        inert: element.hasAttribute('inert'), ariaHidden: element.getAttribute('aria-hidden'),
      });
    }
    if (element === top.mask) vcsModalRestore(element, vcsModalBackground.get(element));
    else { element.setAttribute('inert', ''); element.setAttribute('aria-hidden', 'true'); }
  });
}

function vcsModalKeydown(event) {
  const top = vcsModalStack.length ? vcsModalStack[vcsModalStack.length - 1] : null;
  if (!top) return;
  if (event.key === 'Escape') {
    event.preventDefault(); event.stopPropagation(); top.dismiss(); return;
  }
  if (event.key !== 'Tab') return;
  const focusable = vcsModalFocusable(top.el);
  if (!focusable.length) { event.preventDefault(); top.el.focus(); return; }
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || !top.el.contains(document.activeElement))) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !top.el.contains(document.activeElement))) {
    event.preventDefault(); first.focus();
  }
}

document.addEventListener('keydown', vcsModalKeydown, true);

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
      return { error: VCS.t('bridge.method_missing', { method }, '桥方法不存在：{method}') };
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
        title: VCS.t ? VCS.t('cluster.password.title', {}, '集群密码') : '集群密码',
        bodyHTML:
          '<label class="sr-only" for="pw" data-i18n="cluster.password.label">集群密码</label>' +
          `<input id="pw" type="password" class="ipt" data-i18n-ph="cluster.password.placeholder" ` +
          `placeholder="${VCS.esc(VCS.t('cluster.password.placeholder', {},
            '输入密码(成功后存入系统凭据库)'))}" autocomplete="off">`,
        actions: [
          { label: VCS.t ? VCS.t('common.cancel', {}, '取消') : '取消', quiet: true,
            onClick: mm => { mm.close(); finish(null); } },
          { label: VCS.t ? VCS.t('cluster.connect', {}, '连接') : '连接', primary: true, onClick: mm => {
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
    const dialogTitle = String(title || '').trim();
    if (!dialogTitle) throw new Error('modal title is required for an accessible dialog');
    const returnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    const card = document.createElement('div');
    card.className = 'modal';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.tabIndex = -1;
    const titleId = 'vcs-dialog-title-' + Math.random().toString(36).slice(2);
    card.innerHTML =
      `<div class="m-title" id="${titleId}"></div>` +
      `<div class="m-body"></div>` +
      `<div class="m-actions"></div>`;
    card.setAttribute('aria-labelledby', titleId);
    mask.appendChild(card);

    card.querySelector('.m-title').textContent = dialogTitle;
    const bodyEl = card.querySelector('.m-body');
    if (body instanceof Node) bodyEl.appendChild(body);
    else bodyEl.innerHTML = bodyHTML;

    const handle = { el: card, mask, onDismiss: null, returnFocus };
    let closed = false;
    handle.close = () => {
      if (closed) return;
      closed = true;
      const index = vcsModalStack.indexOf(handle);
      const wasTop = index === vcsModalStack.length - 1;
      if (index >= 0) vcsModalStack.splice(index, 1);
      if (mask.parentNode) mask.parentNode.removeChild(mask);
      vcsModalSyncBackground();
      if (wasTop) {
        const previous = vcsModalStack.length ? vcsModalStack[vcsModalStack.length - 1].el : returnFocus;
        if (previous && previous.isConnected && typeof previous.focus === 'function') {
          try { previous.focus({ preventScroll: true }); } catch (_) { previous.focus(); }
        }
      }
    };
    handle.dismiss = () => {
      if (closed) return;
      if (handle.onDismiss) handle.onDismiss();
      handle.close();
    };

    const actEl = card.querySelector('.m-actions');
    (actions.length ? actions : [{
      label: VCS.t ? VCS.t('common.close', {}, '关闭') : '关闭',
      quiet: true, onClick: h => h.close(),
    }])
      .forEach(a => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn' + (a.primary ? ' primary' : a.quiet ? ' quiet' : '');
        b.textContent = a.label;
        b.addEventListener('click', () => a.onClick ? a.onClick(handle) : handle.close());
        actEl.appendChild(b);
      });

    // 点遮罩空白处 = 取消(触发 onDismiss 后关闭)
    mask.addEventListener('mousedown', e => {
      if (e.target === mask) handle.dismiss();
    });

    document.body.appendChild(mask);
    vcsModalStack.push(handle); vcsModalSyncBackground();
    const initialFocus = card.querySelector(
      '[autofocus],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),' +
      'button:not([disabled]),a[href]');
    (initialFocus || card).focus();
    return handle;
  },

  // 确认框,Promise<boolean>
  confirm(msg) {
    return new Promise(res => {
      let done = false;
      const finish = v => { if (!done) { done = true; res(v); } };
      const m = VCS.modal({
        title: VCS.t ? VCS.t('common.confirm', {}, '请确认') : '请确认',
        bodyHTML: `<div></div>`,
        actions: [
          { label: VCS.t ? VCS.t('common.cancel', {}, '取消') : '取消', quiet: true,
            onClick: mm => { mm.close(); finish(false); } },
          { label: VCS.t ? VCS.t('common.ok', {}, '确定') : '确定', primary: true,
            onClick: mm => { mm.close(); finish(true); } },
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
    const ok = await VCS.confirm(VCS.t('cluster.host_key.confirm', {
      host, algorithm: algorithm || VCS.t('common.not_provided', {}, '未提供'), fingerprint,
    }, '首次连接服务器，请与管理员提供的信息逐字核对：\n\n主机：{host}\n算法：{algorithm}' +
      '\nSHA256 指纹：{fingerprint}\n\n只有完全一致时才选择“确定”。'));
    return ok ? { host, fingerprint, algorithm } : null;
  },

  // 短提示条,2.6s 自动消失。kind: '' | 'ok' | 'fail'
  toast(msg, kind = '') {
    const t = document.createElement('div');
    t.className = 'toast' + (kind ? ' ' + kind : '');
    t.setAttribute('role', kind === 'fail' ? 'alert' : 'status');
    t.setAttribute('aria-live', kind === 'fail' ? 'assertive' : 'polite');
    t.setAttribute('aria-atomic', 'true');
    t.textContent = String(msg);
    document.body.appendChild(t);
    setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 2600);
  },

  // 状态 → pill HTML(类名同 mockup)
  pill(state) {
    const M = {
      RUNNING: ['run', 'RUN'], QUEUED: ['q', 'QUEUE'], SUBMITTED: ['q', 'SUBMIT'],
      UPLOADED: ['q', 'UPLOAD'], DONE: ['ok', 'DONE'], FAILED: ['fail', 'FAIL'],
      UNCONVERGED: ['fail', '未收敛'], NEEDS_HUMAN: ['warn', '需人工'], CREATED: ['q', '待提交'],
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
  if (name === 'ai' && VCS.workspace && VCS.workspace.assistant) {
    return VCS.workspace.assistant.open(sourceLink || null);
  }
  const section = Array.from(document.querySelectorAll('main section[data-page]'))
    .find(s => s.dataset.page === name && !s.hasAttribute('data-shell-assistant'));
  const link = sourceLink || Array.from(document.querySelectorAll('nav a[data-page]'))
    .find(a => a.dataset.page === name);
  if (!name || !section) return false;
  // 工作模式是访问闸，不只是视觉隐藏。许可只由场景白名单或当前计算的
  // 明确路由给出，不依赖某一个导航适配器此刻是否被折叠。
  if (typeof VCS.canActivatePage !== 'function' || !VCS.canActivatePage(name)) {
    VCS.toast('当前工作模式不需要此页面；可在“设置 → 本次计算”切换模式', 'fail');
    return false;
  }
  if (!VCS.workspace) {
    document.querySelectorAll('nav a').forEach(x => x.classList.toggle('on', x === link));
  }
  document.querySelectorAll('main section[data-page]:not([data-shell-assistant])').forEach(
    s => { s.hidden = s.dataset.page !== name; });
  document.dispatchEvent(new CustomEvent('vcs:page', {
    detail: Object.assign({}, detail || {}, { page: name }),
  }));
  return true;
}
VCS.activatePage = activatePage;

document.addEventListener('click', e => {
  const a = e.target.closest('a[data-page]');
  if (!a || a.hasAttribute('data-route')) return;
  e.preventDefault();
  VCS.navigate(a.dataset.page, { source: 'legacy-link' });
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
VCS.focusPendingJob = focusPendingJob;

VCS.focusNavigationTarget = async function (options = {}) {
  if (options.focusJobDir) return focusPendingJob(options.focusJobDir);
  if (!options.focusSelector) return true;
  const el = document.querySelector(options.focusSelector);
  if (!el) return false;
  const card = el.matches && el.matches('[data-acc]')
    ? el : el.closest && el.closest('[data-acc]');
  if (card && VCS.ui && typeof VCS.ui.setAccordionOpen === 'function') {
    VCS.ui.setAccordionOpen(card, true, true);
  } else if (card) {
    card.setAttribute('data-open', '1');
    const toggle = card.querySelector(':scope > .acc-h > .acc-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
  }
  if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' });
  if (typeof el.focus === 'function') {
    if (!el.hasAttribute('tabindex') && !/^(A|BUTTON|INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
      el.tabIndex = -1;
    }
    el.focus();
  }
  return true;
};

// 程序化导航公开入口。options.focusJobDir 专用于“生成 → 提交”的待提交作业聚焦。
VCS.navigate = async function (page, options = {}) {
  if (VCS.workspace && typeof VCS.workspace.navigateLegacy === 'function' &&
      options.workspaceBypass !== true) {
    return VCS.workspace.navigateLegacy(page, options);
  }
  const ok = activatePage(page, null, { source: options.source || 'programmatic' });
  if (!ok) return { ok: false, focused: false };
  const focused = await VCS.focusNavigationTarget(options);
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
            VCS.toast(VCS.t('next_step.open_failed', {
              error: err && err.message ? err.message : err,
            }, '无法打开下一步：{error}'), 'fail');
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

// ── 视觉密度:三档尺寸 token；与主题颜色、科学数据和图表坐标完全独立 ──
// <head> 会先按严格白名单预绘制；桥就绪后再用服务端设置校准并回写本地首帧缓存。
const DENSITIES = ['comfortable', 'standard', 'compact'];
VCS.densityApply = function (name) {
  const density = DENSITIES.indexOf(name) >= 0 ? name : 'standard';
  document.documentElement.dataset.density = density;
  try { localStorage.setItem('vcs.density', density); } catch (_) { /* 隐私模式忽略 */ }
  document.dispatchEvent(new CustomEvent('vcs:density', { detail: { density } }));
  return density;
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
      const title = VCS.t('chemistry.metal_site', { symbol: sym }, '金属位：{symbol}');
      return `<span class="elbadge" style="--el:${EL_CPK[sym]}" title="${VCS.esc(title)}">${sym}</span>`;
    }
  }
  return '';   // 识别不出不加(不猜)
};

// ── 自动托管观察器:计算由 Python 后台线程驱动；页面只读状态并渲染 ─────────
VCS.pipeline = {
  timer: null, running: false, failStreak: 0, lastSuccess: null,
  events: [], runtime: null, lastFinished: null, lastOutcomeSeq: 0,
  eventsSeen: new Set(),   // 最近 20 条(新在前),按后台一拍只消费一次
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
    el.innerHTML = `<span class="dot g"></span>${VCS.esc(VCS.t('pipeline.sync_failed_count', {
      count: p.failStreak,
    }, '同步失败×{count}'))}<span class="bridge">${VCS.esc(VCS.t(
      'bridge.healthy', {}, '界面桥 ✓'))}</span>`;
  } else if (p.lastSuccess) {
    el.className = '';
    el.innerHTML = `<span class="dot g"></span>${VCS.esc(VCS.t('pipeline.synced_at', {
      time: hhmm(p.lastSuccess),
    }, '同步 {time} ✓'))}<span class="bridge">${VCS.esc(VCS.t(
      'bridge.healthy', {}, '界面桥 ✓'))}</span>`;
  } else {
    el.className = '';
    el.innerHTML = `<span class="dot g"></span><span class="bridge">${VCS.esc(VCS.t(
      'bridge.healthy', {}, '界面桥 ✓'))}</span>`;
  }
}

function renderConnBanner() {
  const b = document.getElementById('conn-banner');
  if (!b) return;
  const p = VCS.pipeline;
  if (p.failStreak >= 2) {
    b.classList.add('show');
    b.textContent = VCS.t('pipeline.connection_stale', {
      count: p.failStreak,
      last: p.lastSuccess ? hhmm(p.lastSuccess) : '—',
    }, '⚠ 集群连接可能已断：最近 {count} 次同步失败（上次成功 {last}），数据可能过期');
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
    report_blocked: '诊断报告', skip: '跳过', error: '错误' };
  box.innerHTML = evs.map(e => {
    const kcls = e.kind === 'report_done' ? 'report'
      : (e.kind === 'error' ? 'err' : (e.kind === 'skip' ? 'skip' : ''));
    const label = KL[e.kind] || e.kind;
    const cluster = e.cluster
      ? `<span class="fcluster" title="服务器">${VCS.esc(e.cluster)}</span>` : '';
    const btn = ((e.kind === 'report_done' || e.kind === 'report_blocked') &&
      (e.report || e.figures_dir))
      ? `<button class="btn quiet fbtn" data-open="${VCS.esc(e.report || e.figures_dir)}">打开</button>` : '';
    return `<div class="feed-row"><span class="fk ${kcls}">${VCS.esc(label)}</span>${cluster}` +
      `<span class="ftxt" title="${VCS.esc(e.text || '')}">${VCS.esc(e.text || '')}</span>` +
      `<span class="ft">${VCS.esc(e.time || '')}</span>${btn}</div>`;
  }).join('');
}

function reportToast(ev) {
  const t = document.createElement('div');
  t.className = 'toast ok';
  t.textContent = VCS.t('pipeline.report_generated', {
    project: ev.project || '',
  }, '报告已自动生成：{project}');
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
  document.dispatchEvent(new CustomEvent('vcs:pipeline-events', {
    detail: { events: p.events.slice(), outcome: out },
  }));
  if (other) VCS.toast(VCS.t('pipeline.events_summary', {
    count: other,
  }, '自动托管：本轮 {count} 条动态'), '');
  if ((out.errors || []).length) VCS.toast(VCS.t('pipeline.errors_summary', {
    count: out.errors.length,
  }, '自动托管遇到 {count} 个问题（见任务动态）'), 'fail');
  renderHealth();
  renderConnBanner();
  renderFeed();
}

async function pipelineRuntimePoll() {
  const p = VCS.pipeline;
  if (p.running) return;
  p.running = true;
  try {
    const res = await VCS.call('pipeline_runtime_status');
    if (!res || res.error || !res.state) {
      p.failStreak++;
      renderHealth(); renderConnBanner();
      return;
    }
    const state = res.state;
    p.runtime = state;
    const finished = String(state.last_finished || '');
    const sequence = Number(state.outcome_seq || 0);
    const history = Array.isArray(state.outcome_history)
      ? state.outcome_history
          .filter(item => Number(item && item.seq) > p.lastOutcomeSeq)
          .sort((a, b) => Number(a.seq) - Number(b.seq))
      : [];
    if (history.length) {
      history.forEach(item => {
        const itemSequence = Number(item.seq || 0);
        if (itemSequence) p.lastOutcomeSeq = Math.max(p.lastOutcomeSeq, itemSequence);
        if (item.finished) p.lastFinished = String(item.finished);
        if (item.outcome) {
          onPipelineOutcome(item.outcome);
        } else if (item.error) {
          p.events.unshift({
            kind: 'error',
            text: VCS.t('pipeline.runtime_error', {
              error: String(item.error),
            }, '后台自动托管异常：{error}'),
            time: hhmm(item.finished),
          });
          p.events = p.events.slice(0, 20);
        }
      });
      renderHealth(); renderConnBanner(); renderFeed();
    } else {
      // 兼容没有 outcome_history 的旧后端；新后端即使两轮检查落在一次
      // 前端轮询之间，也会按 seq 顺序逐条消费，而不会只看到最后一轮。
      const hasNewOutcome = sequence
        ? sequence > p.lastOutcomeSeq
        : (finished && finished !== p.lastFinished);
      if (hasNewOutcome && state.last_outcome) {
        p.lastOutcomeSeq = sequence;
        p.lastFinished = finished;
        onPipelineOutcome(state.last_outcome);
      } else {
        if (sequence) p.lastOutcomeSeq = Math.max(p.lastOutcomeSeq, sequence);
        if (finished) p.lastFinished = finished;
        renderHealth(); renderConnBanner(); renderFeed();
      }
    }
    document.dispatchEvent(new CustomEvent('vcs:pipeline-runtime', { detail: state }));
  } finally {
    p.running = false;
  }
}

async function pipelineTick() {
  const queued = await VCS.call('pipeline_wake');
  if (!queued || queued.error) {
    VCS.toast(VCS.t('pipeline.wake_failed', {
      error: (queued && queued.error) || VCS.t('common.unknown_error', {}, '未知错误'),
    }, '无法启动后台检查：{error}'), 'fail');
    return queued;
  }
  setTimeout(pipelineRuntimePoll, 350);
  return queued;
}

VCS.pipeline.tick = pipelineTick;
VCS.pipeline.poll = pipelineRuntimePoll;
VCS.pipeline.renderFeed = renderFeed;

// 从 config 校准主题；后台调度器读同一配置，前端只保持轻量状态轮询。
VCS.pipeline.reconfigure = async function () {
  const s = await VCS.call('settings_get');
  const ui = (s && s.ui) || {};
  if (ui.theme) VCS.themeApply(ui.theme);
  if (ui.density) VCS.densityApply(ui.density);
  else if (!document.documentElement.dataset.density) VCS.densityApply('standard');
  const enabled = ui.autopilot === true;
  if (VCS.pipeline.timer) { clearInterval(VCS.pipeline.timer); VCS.pipeline.timer = null; }
  VCS.pipeline.timer = setInterval(pipelineRuntimePoll, 5000);
  await pipelineRuntimePoll();
  // 设置保存和整组提交由后端在同一事务后唤醒主管；这里只更新观察器，
  // 避免前后端各 wake 一次导致同秒重复检查。
  if (!enabled) renderHealth();
};

// feed 内「打开」按钮委托 + 切回仪表盘时重渲 feed
document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('#db-feed [data-open]');
  if (b) { e.preventDefault(); VCS.call('open_dir', b.dataset.open); }
});
document.addEventListener('vcs:page', e => {
  if (e.detail && e.detail.page === 'dashboard') renderFeed();
});

// ── i18n:显式键 + 旧页面精确短语映射；不调用在线翻译，也不猜测科学文本。 ──
const I18N_RAW_SELECTOR = 'script,style,code,pre,textarea,.log,.log-box,[data-i18n-raw]';
const I18N_ATTRIBUTES = Object.freeze({
  'data-i18n-ph': 'placeholder', 'data-i18n-title': 'title',
  'data-i18n-aria-label': 'aria-label',
  'data-i18n-aria-description': 'aria-description', 'data-i18n-alt': 'alt',
});
const i18nTextSource = new WeakMap();
const i18nAttributeSource = new WeakMap();
VCS.i18n = { dict: {}, source: {}, reverse: new Map(), lang: 'zh', observer: null };

function i18nFormat(value, params) {
  let text = String(value == null ? '' : value);
  Object.entries(params || {}).forEach(([key, replacement]) => {
    text = text.replace(new RegExp('\\{' + String(key).replace(/[.*+?^${}()|[\\]\\]/g, '\\$&') + '\\}', 'g'),
      String(replacement));
  });
  return text;
}

VCS.t = function (key, params, fallback) {
  const table = VCS.i18n.dict || {};
  const value = Object.prototype.hasOwnProperty.call(table, key) ? table[key]
    : (fallback == null ? key : fallback);
  return i18nFormat(value, params);
};

function rebuildI18nReverse() {
  const source = VCS.i18n.source || {};
  const reverse = new Map();
  Object.keys(source).sort().forEach(key => {
    const value = source[key];
    if (typeof value === 'string' && value.trim() && !reverse.has(value.trim())) {
      reverse.set(value.trim(), key);
    }
  });
  VCS.i18n.reverse = reverse;
}

function translatedLegacyText(source) {
  const trimmed = String(source || '').trim();
  const key = VCS.i18n.reverse.get(trimmed);
  return key ? VCS.t(key, {}, trimmed) : source;
}

function translateLegacyTextNode(node) {
  const parent = node && node.parentElement;
  if (!parent || parent.closest(I18N_RAW_SELECTOR)) return;
  let source = i18nTextSource.get(node);
  if (source == null) {
    const candidate = String(node.nodeValue || '');
    if (!VCS.i18n.reverse.has(candidate.trim())) return;
    source = candidate; i18nTextSource.set(node, source);
  }
  const leading = source.match(/^\s*/)[0]; const trailing = source.match(/\s*$/)[0];
  const translated = translatedLegacyText(source.trim());
  const next = leading + translated + trailing;
  if (node.nodeValue !== next) node.nodeValue = next;
}

function translateLegacyAttributes(element) {
  if (!element || element.closest(I18N_RAW_SELECTOR)) return;
  const attributes = ['placeholder', 'title', 'aria-label', 'aria-description', 'alt'];
  let originals = i18nAttributeSource.get(element);
  if (!originals) { originals = {}; i18nAttributeSource.set(element, originals); }
  attributes.forEach(name => {
    if (!element.hasAttribute(name)) return;
    if (Object.values(I18N_ATTRIBUTES).includes(name) &&
        Object.entries(I18N_ATTRIBUTES).some(([marker, attr]) => attr === name && element.hasAttribute(marker))) return;
    if (!Object.prototype.hasOwnProperty.call(originals, name)) {
      const candidate = element.getAttribute(name) || '';
      if (!VCS.i18n.reverse.has(candidate.trim())) return;
      originals[name] = candidate;
    }
    const next = translatedLegacyText(originals[name]);
    if (element.getAttribute(name) !== next) element.setAttribute(name, next);
  });
}

function translateI18nSubtree(root) {
  const scope = root && root.nodeType === Node.ELEMENT_NODE ? root : document;
  const elements = scope === document ? Array.from(document.querySelectorAll('*'))
    : [scope, ...scope.querySelectorAll('*')];
  elements.forEach(element => {
    const textKey = element.getAttribute && element.getAttribute('data-i18n');
    // `textContent` on a container would delete its input/select/button descendants.
    // Authored translation anchors are required to be leaves; fail closed if a future
    // template violates that contract, while the static semantic test identifies it.
    if (textKey && element.childElementCount === 0) {
      element.textContent = VCS.t(textKey, {}, element.textContent);
    }
    Object.entries(I18N_ATTRIBUTES).forEach(([marker, attribute]) => {
      const key = element.getAttribute && element.getAttribute(marker);
      if (key) element.setAttribute(attribute, VCS.t(key, {}, element.getAttribute(attribute) || ''));
    });
    translateLegacyAttributes(element);
  });
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  let textNode;
  while ((textNode = walker.nextNode())) translateLegacyTextNode(textNode);
}

VCS.applyI18n = function (dict, source) {
  if (dict) VCS.i18n.dict = dict;
  if (source) VCS.i18n.source = source;
  rebuildI18nReverse(); translateI18nSubtree(document);
  if (!VCS.i18n.observer) {
    VCS.i18n.observer = new MutationObserver(records => records.forEach(record => {
      record.addedNodes.forEach(node => {
        if (node.nodeType === Node.TEXT_NODE) translateLegacyTextNode(node);
        else if (node.nodeType === Node.ELEMENT_NODE) translateI18nSubtree(node);
      });
      if (record.type === 'attributes') translateLegacyAttributes(record.target);
    }));
    VCS.i18n.observer.observe(document.documentElement, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ['placeholder', 'title', 'aria-label', 'aria-description', 'alt'],
    });
  }
};
VCS.loadLang = async function (lang) {
  let lg = lang;
  if (!lg) { const g = await VCS.call('lang_get'); lg = (g && g.lang) || 'zh'; }
  const r = await VCS.call('i18n_dict', lg);
  if (!(r && r.ok && r.dict && r.source)) throw new Error((r && r.error) || 'language bundle unavailable');
  lg = r.lang === 'en' ? 'en' : 'zh'; VCS.i18n.lang = lg;
  VCS.applyI18n(r.dict, r.source);
  try { document.documentElement.lang = (lg === 'en' ? 'en' : 'zh-CN'); } catch (_) { /* 忽略 */ }
  document.dispatchEvent(new CustomEvent('vcs:language', { detail: { lang: lg } }));
  return lg;
};

// ── 工作模式:按 data-scene 点分路径显隐 nav 项与卡片(镜像 scenarios.is_visible 口径) ──
VCS.scenario = null;
VCS.activeEngine = 'vasp';
VCS.engineCapability = {};
VCS.activeCalculation = '';

// “本次计算类型”不仅裁剪卡片，也必须给出一个真实可到达的首要入口。
// 大多数 VASP 任务从生成页派生；三个结果型流程直接去项目页，自旋扫描去结构页。
// 这份前端路由不替代后端 task_keys 白名单，只负责把已获准的类型送到正确页面。
const CALCULATION_ROUTES = {
  adsorption_project: { page: 'project', focusSelector: '#ads-journey', analysis: 'adsorption' },
  spin_scan: { page: 'structure', focusSelector: '#spin-card' },
  formation_binding: { page: 'project', focusSelector: '#fb-card', analysis: 'taskana' },
  surface_energy: { page: 'project', focusSelector: '#ta-se-slab', analysis: 'taskana' },
};

VCS.calculationRoute = function (key, scenario) {
  const task = String(key || VCS.activeCalculation || '');
  const sc = scenario || VCS.scenario;
  if (!task) return null;
  // 场景白名单仍是访问闸；不能靠前端路由打开不适用的计算。
  if (sc && Array.isArray(sc.task_keys) && sc.task_keys.indexOf(task) < 0) return null;
  if (CALCULATION_ROUTES[task]) return Object.assign({}, CALCULATION_ROUTES[task]);
  if (VCS.activeEngine === 'gaussian') {
    return { page: 'generate', focusSelector: '#gauss-panel' };
  }
  if (VCS.activeEngine && VCS.activeEngine !== 'vasp') {
    return { page: 'generate', focusSelector: '#engine-card' };
  }
  return {
    page: 'generate',
    focusSelector: task === 'neb' ? '#neb-card' : '#taskcat-card',
  };
};

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

// 物理页面访问许可：场景原生页面白名单是基础；场景已授权的“本次计算”可
// 额外放行其唯一目标页。calculationRoute 自身会先核验 task_keys，因此手工
// 改 DOM 属性、直接改 hash 或程序化调用都不能放行其他页面。
function pageAllowed(page, scenario) {
  const name = String(page || '');
  if (!name) return false;
  const sc = (scenario === undefined) ? VCS.scenario : scenario;
  if (!sc) return true;
  if (sceneVisible(sc, 'pages.' + name)) return true;
  const route = VCS.calculationRoute(VCS.activeCalculation, sc);
  return !!route && route.page === name;
}
VCS.pageAllowed = pageAllowed;

// 无副作用的壳层路由预检。workspace.js 在写入 History 前调用；这里只有
// DOM/状态读取，不切页、不改 hash，也不显示提示。
VCS.canActivatePage = function (page, scenario) {
  const name = String(page || '');
  if (!name) return false;
  const section = Array.from(document.querySelectorAll('main section[data-page]'))
    .find(s => s.dataset.page === name && !s.hasAttribute('data-shell-assistant'));
  if (!section) return false;
  return pageAllowed(name, scenario === undefined ? VCS.scenario : scenario);
};

function applySceneElements(sc) {
  if (!sc) return;
  document.querySelectorAll('[data-scene]').forEach(el => {
    el.toggleAttribute('data-scene-hidden', !sceneVisible(sc, el.getAttribute('data-scene')));
  });
  // 允许的引擎必须至少有一个真实输入入口。比如“分子化学”默认收起 VASP
  // 四件套，但用户显式改选 VASP 后应恢复 VASP 输入，而不是只剩一个空页面。
  if ((sc.engines || []).indexOf(VCS.activeEngine) >= 0) {
    document.querySelectorAll('[data-engine][data-scene^="cards.generate."]').forEach(el => {
      const engines = String(el.getAttribute('data-engine') || '').split(/\s+/);
      if (engines.indexOf(VCS.activeEngine) >= 0) el.removeAttribute('data-scene-hidden');
    });
  }
  // 某些专用模式（例如 Li-S）默认精简掉“生成输入”。当用户在设置里明确
  // 改选 NEB / DOS / 收敛扫描等类型时，只放行该类型真正需要的目标页。
  const route = VCS.calculationRoute(VCS.activeCalculation, sc);
  if (route) {
    document.querySelectorAll(`nav a[data-page="${route.page}"]`).forEach(
      link => link.removeAttribute('data-scene-hidden'));
  }
}

function applyEngineElements(engine) {
  const active = String(engine || 'vasp');
  document.querySelectorAll('[data-engine]').forEach(el => {
    const tokens = String(el.getAttribute('data-engine') || '').split(/\s+/).filter(Boolean);
    el.toggleAttribute('data-engine-hidden', tokens.indexOf(active) < 0 && tokens.indexOf('all') < 0);
  });
}

function refreshModeChip() {
  const chip = document.getElementById('mode-chip');
  if (!chip) return;
  const sc = VCS.scenario || {};
  const engine = String(VCS.activeEngine || 'vasp').toUpperCase();
  const scenarioName = (VCS.i18n.lang === 'en' && sc.name_en) || sc.name || sc.key ||
    VCS.t('settings.scenario.label', {}, '工作模式', 'Workflow mode');
  chip.textContent = scenarioName + ' · ' + engine;
  chip.title = VCS.t(
    'legacy.dynamic.app.0010', {},
    '切换工作模式、计算引擎或本次计算类型',
    'Switch workflow mode, compute engine, or calculation type');
}

document.addEventListener('vcs:language', refreshModeChip);

let scenarioNavigationGeneration = 0;

function keepCurrentPageReachable(sc, source) {
  const generation = ++scenarioNavigationGeneration;
  const current = document.querySelector(
    'main section[data-page]:not([data-shell-assistant]):not([hidden])');
  if (!current || pageAllowed(current.dataset.page, sc)) return;
  const route = VCS.calculationRoute(VCS.activeCalculation, sc);
  const landing = (route && route.page) ||
    (sc.defaults && sc.defaults.landing_page) || 'dashboard';
  if (VCS.workspace && typeof VCS.workspace.navigateLegacy === 'function') {
    VCS.workspace.navigateLegacy(landing, { source, replace: true }).then(out => {
      if (generation !== scenarioNavigationGeneration || VCS.scenario !== sc) return;
      if (!out || !out.ok) {
        VCS.workspace.navigateLegacy('dashboard', {
          source: source + '-fallback', replace: true, force: true,
        });
      }
    });
  } else if (!activatePage(landing, null, { source })) {
    activatePage('dashboard', null, { source: source + '-fallback' });
  }
}

VCS.applyScenario = function (sc) {
  if (sc) VCS.scenario = sc;
  const s = VCS.scenario;
  if (!s) return;
  applySceneElements(s);
  applyEngineElements(VCS.activeEngine);
  refreshModeChip();
  // 当前页被模式隐藏 → 优先去所选计算的真实入口；再按模式默认页回退。
  keepCurrentPageReachable(s, 'work-mode');
  document.dispatchEvent(new CustomEvent('vcs:scenario', { detail: { scenario: s } }));
};

VCS.applyEngine = function (engine, capability) {
  VCS.activeEngine = String(engine || 'vasp').toLowerCase();
  VCS.engineCapability = capability || {};
  if (VCS.scenario) applySceneElements(VCS.scenario);
  applyEngineElements(VCS.activeEngine);
  refreshModeChip();
  document.dispatchEvent(new CustomEvent('vcs:engine', {
    detail: { engine: VCS.activeEngine, capability: VCS.engineCapability },
  }));
};

VCS.applyCalculation = function (key) {
  VCS.activeCalculation = String(key || '');
  document.querySelectorAll('[data-task]').forEach(el => {
    const tokens = String(el.getAttribute('data-task') || '').split(/\s+/).filter(Boolean);
    el.toggleAttribute('data-task-hidden', !!VCS.activeCalculation &&
      tokens.indexOf(VCS.activeCalculation) < 0 && tokens.indexOf('all') < 0);
  });
  // 重新按场景计算页面显隐，既能放行新类型需要的页，也会在切回吸附能时
  // 收起此前临时放行的生成页。
  if (VCS.scenario) {
    applySceneElements(VCS.scenario);
    keepCurrentPageReachable(VCS.scenario, 'calculation-type');
  }
  document.dispatchEvent(new CustomEvent('vcs:calculation', {
    detail: { activeCalculation: VCS.activeCalculation },
  }));
};

// 工作模式、引擎、计算类型是一个服务端 revisioned snapshot。前端 generation
// 只过滤本窗口中过期的响应；真正的覆盖保护由 expected_revision CAS 提供。
const workspaceContextState = {
  revision: null,
  context: null,
  generation: 0,
  session: (window.crypto && typeof window.crypto.randomUUID === 'function')
    ? window.crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
};
VCS.workspaceContext = workspaceContextState;

VCS.applyWorkspaceContext = function (result) {
  const context = result && result.context;
  if (!context || !Number.isInteger(context.revision)) return false;
  if (Number.isInteger(workspaceContextState.revision) &&
      context.revision < workspaceContextState.revision) return false;
  if (workspaceContextState.context &&
      context.revision === workspaceContextState.revision) return false;
  workspaceContextState.revision = context.revision;
  workspaceContextState.context = context;
  // Publish every raw value before any legacy per-field event fires.  Event
  // listeners therefore always observe the same authoritative tuple even
  // though the compatibility render hooks remain separate.
  VCS.scenario = context.scenario;
  VCS.activeEngine = String(context.engine || 'vasp').toLowerCase();
  VCS.engineCapability = context.capability || {};
  VCS.activeCalculation = String(context.calculation || '');
  VCS.applyScenario(context.scenario);
  VCS.applyEngine(context.engine || 'vasp', context.capability || {});
  VCS.applyCalculation(context.calculation || '');
  return true;
};

VCS.loadWorkspaceContext = async function () {
  const result = await VCS.call('settings_context_get');
  if (result && result.ok) VCS.applyWorkspaceContext(result);
  return result;
};

VCS.updateWorkspaceContext = async function (patch) {
  const generation = ++workspaceContextState.generation;
  const intentId = `${workspaceContextState.session}:${generation}`;
  if (!Number.isInteger(workspaceContextState.revision)) {
    const loaded = await VCS.loadWorkspaceContext();
    if (generation !== workspaceContextState.generation) {
      return Object.assign({}, loaded || {}, { ok: false, superseded: true });
    }
    if (!(loaded && loaded.ok)) return loaded;
  }
  const result = await VCS.call(
    'settings_context_update', patch, workspaceContextState.revision, intentId);
  if (generation !== workspaceContextState.generation) {
    return Object.assign({}, result || {}, { ok: false, superseded: true });
  }
  // A conflict is a completed server decision, not permission to replay the
  // old patch against a newer revision.  Adopt the authority snapshot and
  // require another explicit user action to create a new intent.
  if (result && result.context) VCS.applyWorkspaceContext(result);
  return result;
};

// 设置页与仪表盘共用：进入所选计算的唯一主入口，并展开/聚焦目标卡片。
VCS.openCalculation = async function (key, options = {}) {
  const task = String(key || VCS.activeCalculation || '');
  const route = VCS.calculationRoute(task, VCS.scenario);
  if (!route) {
    VCS.toast('当前工作模式不支持这个计算类型，请回到设置重新选择', 'fail');
    return { ok: false, focused: false };
  }
  const out = await VCS.navigate(route.page, { source: options.source || 'calculation-entry' });
  if (!out.ok) return out;
  if (route.analysis) {
    const analysis = document.getElementById('analysis-type');
    if (analysis && analysis.value !== route.analysis) {
      analysis.value = route.analysis;
      analysis.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  if (task === 'adsorption_project' && options.startFresh && window.Project &&
      typeof window.Project.startLiS === 'function') {
    await window.Project.startLiS();
    return { ok: true, focused: true };
  }
  const target = route.focusSelector && document.querySelector(route.focusSelector);
  if (target) {
    const card = target.classList && target.classList.contains('acc')
      ? target : target.closest && target.closest('.acc');
    if (card && VCS.ui && typeof VCS.ui.setAccordionOpen === 'function') {
      VCS.ui.setAccordionOpen(card, true, true);
    } else if (card) {
      card.setAttribute('data-open', '1');
      const toggle = card.querySelector(':scope > .acc-h > .acc-toggle');
      if (toggle) toggle.setAttribute('aria-expanded', 'true');
    }
    if (typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    if (typeof target.focus === 'function') target.focus();
  }
  return { ok: true, focused: !!target };
};

VCS.loadCalculation = async function () {
  await VCS.loadWorkspaceContext();
  return VCS.activeCalculation;
};

VCS.loadEngine = async function () {
  const r = await VCS.loadWorkspaceContext();
  if (!(r && r.ok)) VCS.applyEngine('vasp', {});
  return VCS.activeEngine;
};

// ── 首启工作模式选择模态(config 无 ui.scenario 时;只给四个常用模式) ──
async function firstLaunchScenario() {
  const r = await VCS.call('scenario_list');
  const list = ((r && r.scenarios) || []).filter(s => s.primary)
    .sort((a, b) => (a.key === 'vasp' ? -1 : b.key === 'vasp' ? 1 : 0));
  if (!list.length) return;
  const box = document.createElement('div');
  box.innerHTML = '<div class="scene-grid">' + list.map(s =>
    `<button type="button" class="scene-card" data-key="${VCS.esc(s.key)}"><b>${VCS.esc((VCS.i18n.lang === 'en' && s.name_en) || s.name)}${s.key === 'vasp' ? VCS.esc(VCS.t('common.recommended_suffix', {}, '（推荐）')) : ''}</b>` +
    `<span>${VCS.esc((VCS.i18n.lang === 'en' && s.description_en) || s.description)}</span></button>`).join('') + '</div>';
  const m = VCS.modal({ title: VCS.t('scenario.first_launch.title', {}, '这次要做哪类计算？（之后可在设置中切换）'),
    body: box, actions: [] });
  m.el.classList.add('modal-wide');
  box.querySelectorAll('.scene-card').forEach(c => c.addEventListener('click', async () => {
    const key = c.dataset.key;
    m.close();
    const res = await VCS.updateWorkspaceContext({ scenario: key });
    const context = res && res.context;
    if (res && res.ok && context && context.scenario) {
      const scenario = context.scenario;
      const name = (VCS.i18n.lang === 'en' && scenario.name_en) || scenario.name || key;
      VCS.toast(VCS.t('scenario.selected', { name }, '已选择工作模式：{name}'));
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
      const sc = await VCS.loadWorkspaceContext();
      const context = sc && sc.context;
      if (sc && sc.ok && context && context.scenario) {
        if (!(context.configured && context.configured.scenario)) {
          firstLaunchScenario();   // 首启弹场景选择模态
        }
      }
    } catch (_) { /* 场景失败不挡界面 */ }
  } catch (_) {
    const el = document.getElementById('conn');
    if (el) el.innerHTML = '<span class="dot g"></span><span class="bridge">界面桥 响应异常</span>';
  }
});
