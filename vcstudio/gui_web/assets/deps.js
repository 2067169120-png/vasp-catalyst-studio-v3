// deps.js — 侧栏依赖状态区(对齐 starpivot 左下角)+ 下载/修复依赖弹窗 + 概览核时四卡。
// deps_status 汇总各 probe;deps_install 后台 pip + deps_install_status 轮询;overview_stats 聚合。
// 只依赖 app.js 的 VCS.*;插值走 VCS.esc,零 emoji。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const State = { deps: [], pollTimer: null, runtimeInstallSupported: true, installNote: '' };

  // ── 侧栏依赖状态 ──
  async function loadDeps() {
    const r = await VCS.call('deps_status');
    const box = $('deps-list');
    if (!box) return;
    if (!r || r.ok === false) { box.innerHTML = '<span class="deps-empty">检测失败</span>'; return; }
    State.deps = r.deps || [];
    State.runtimeInstallSupported = r.runtime_install_supported !== false;
    State.installNote = r.runtime_install_note || '';
    box.innerHTML = State.deps.map(d =>
      `<div class="deps-item ${d.available ? 'ok' : 'bad'}" title="${VCS.esc(d.detail || '')}">` +
      `<span class="dot"></span><span class="dnm">${VCS.esc(d.name)}</span>` +
      `<span class="dst">${d.available ? '✓' : '—'}</span></div>`).join('');
  }

  // ── 下载/修复依赖弹窗 ──
  function openInstallModal() {
    if (!State.runtimeInstallSupported) {
      const box = document.createElement('div');
      box.className = 'deps-modal';
      const note = document.createElement('p');
      note.className = 'sub';
      note.textContent = State.installNote || '单文件 EXE 的 Python 依赖需在打包时内置。';
      box.appendChild(note);
      VCS.modal({ title: '依赖安装说明', body: box });
      return;
    }
    const installable = State.deps.filter(d => d.installable);
    if (!installable.length) { VCS.toast('无可自动安装的组件'); return; }
    const box = document.createElement('div');
    box.className = 'deps-modal';
    box.innerHTML = installable.map(d =>
      `<label class="deps-opt"><input type="checkbox" data-pkg="${VCS.esc(d.key)}" ${d.available ? '' : 'checked'}>` +
      `<span><b>${VCS.esc(d.name)}${d.available ? '(已装)' : ''}</b><span>${VCS.esc(d.note || '')}</span></span></label>`).join('') +
      '<div class="sub" style="margin-top:6px;color:var(--muted)">后台 pip 安装,进度见下方日志;DECIMER 含 TensorFlow 模型,约数百 MB,请耐心等待。</div>' +
      '<textarea class="ipt deps-log" id="deps-log" readonly hidden></textarea>';
    const m = VCS.modal({
      title: '下载 / 修复依赖', body: box,
      actions: [
        { label: '关闭', quiet: true, onClick: h => { stopPoll(); h.close(); } },
        { label: '开始安装', primary: true, onClick: h => startInstall(box, h) },
      ],
    });
    m.el.classList.add('modal-wide');
  }
  async function startInstall(box, handle) {
    const pkgs = Array.from(box.querySelectorAll('input[data-pkg]:checked')).map(c => c.dataset.pkg);
    if (!pkgs.length) { VCS.toast('请选择要安装的组件'); return; }
    const r = await VCS.call('deps_install', pkgs);
    const log = box.querySelector('#deps-log');
    if (!r || r.ok === false) {
      if (log) { log.hidden = false; log.value = '启动失败:' + ((r && r.error) || '未知'); }
      return;
    }
    if (log) { log.hidden = false; log.value = '正在安装:' + (r.pip || []).join(' ') + ' …'; }
    VCS.log('后台安装依赖:' + (r.pkgs || []).join('、'), 'okc');
    pollInstall(log);
  }
  function stopPoll() { if (State.pollTimer) { clearInterval(State.pollTimer); State.pollTimer = null; } }
  function pollInstall(log) {
    stopPoll();
    State.pollTimer = setInterval(async () => {
      const s = await VCS.call('deps_install_status');
      if (!s || s.ok === false) return;
      if (log && s.log_tail) log.value = s.log_tail;
      if (s.done) {
        stopPoll();
        const ok = s.returncode === 0;
        VCS.log('依赖安装' + (ok ? '完成' : '失败(退出码 ' + s.returncode + ')'), ok ? 'okc' : 'failc');
        VCS.toast(ok ? '依赖安装完成' : '依赖安装失败', ok ? 'ok' : 'fail');
        loadDeps();
      }
    }, 2000);
  }

  // ── 概览核时四卡 ──
  async function loadOverview() {
    const r = await VCS.call('overview_stats');
    if (!r || r.ok === false) return;
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('ov-jobs30', r.jobs_30d != null ? r.jobs_30d : '—');
    set('ov-ch30', r.core_hours_30d != null ? Math.round(r.core_hours_30d) : '—');
    // v3.3.0 实耗核时:时间戳×提交核数(与预算估算并列的独立口径;缺数据作业单列不编数)
    set('ov-used30', r.used_core_hours_30d != null ? Math.round(r.used_core_hours_30d) : '—');
    const usedEl = $('ov-used30');
    if (usedEl) {
      usedEl.title = '实耗 =(RUNNING→终态)时长 × 提交核数;在跑作业实时累计'
        + (r.usage_unknown_n ? ';另有 ' + r.usage_unknown_n + ' 个作业缺核数/时间戳未计入' : '');
    }
    set('ov-remain', r.remaining_core_hours != null ? Math.round(r.remaining_core_hours) : '∞');
    const mon = r.monitor || {};
    set('ov-monitor', mon.status || '—');
    const monEl = $('ov-monitor');
    if (monEl && mon.active) monEl.title = '运行 ' + (mon.running || 0) + ' · 排队 ' + (mon.queued || 0);
  }

  // ── 初始化 ──
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    const fix = $('deps-fix-btn');
    if (fix) fix.addEventListener('click', openInstallModal);
    loadDeps();
  }
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'dashboard') loadOverview();
  });
  VCS.ready.then(() => { init(); loadOverview(); });
  window.Deps = { reload: loadDeps, reloadOverview: loadOverview };
})();
