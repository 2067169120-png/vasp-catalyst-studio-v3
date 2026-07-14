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
    let box = document.querySelector('.log');
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

// ── 路由:nav a[data-page] 点击 → section 显隐 + .on 高亮 ──
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-page]');
  if (!a) return;
  e.preventDefault();
  document.querySelectorAll('nav a').forEach(x => x.classList.toggle('on', x === a));
  document.querySelectorAll('main section[data-page]').forEach(
    s => { s.hidden = s.dataset.page !== a.dataset.page; });
  document.dispatchEvent(new CustomEvent('vcs:page', { detail: { page: a.dataset.page } }));
});

// ── 桥就绪后确认 ping,更新连接态读数 ──
VCS.ready.then(async () => {
  try {
    const pong = await window.pywebview.api.ping();
    const el = document.getElementById('conn');
    if (el) {
      el.innerHTML = pong === 'pong'
        ? '<span class="dot g"></span>本地桥就绪'
        : '<span class="dot g"></span>桥响应异常';
    }
  } catch (_) { /* 桥探测失败不阻塞 UI */ }
});
