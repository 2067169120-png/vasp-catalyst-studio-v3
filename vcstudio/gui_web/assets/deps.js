// deps.js — 侧栏依赖状态 + 可复制安装命令 + 概览核时四卡。
// 依赖弹窗默认只给出明确命令，不在用户不可见的后台启动 pip。
// 只依赖 app.js 的 VCS.*;插值走 VCS.esc,零 emoji。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const State = { deps: [], runtimeInstallSupported: true, installNote: '' };
  const RECOMMENDED = new Set(['rdkit', 'matplotlib', 'python-docx', 'pypdf']);

  // ── 侧栏依赖状态 ──
  async function loadDeps() {
    const r = await VCS.call('deps_status');
    const box = $('deps-list');
    if (!box) return;
    if (!r || r.ok === false) { box.innerHTML = '<span class="deps-empty">检测失败</span>'; return; }
    State.deps = r.deps || [];
    State.runtimeInstallSupported = r.runtime_install_supported !== false;
    State.installNote = r.runtime_install_note || '';
    const fix = $('deps-fix-btn');
    if (fix) fix.textContent = State.runtimeInstallSupported ? '查看 / 复制安装命令' : '查看依赖说明';
    box.innerHTML = State.deps.map(d =>
      `<div class="deps-item ${d.available ? 'ok' : 'bad'}" title="${VCS.esc(d.detail || '')}">` +
      `<span class="dot"></span><span class="dnm">${VCS.esc(d.name)}</span>` +
      `<span class="dst">${d.available ? '✓' : '—'}</span></div>`).join('');
  }

  function selectedCommand(box) {
    const keys = Array.from(box.querySelectorAll('input[data-pkg]:checked')).map(x => x.dataset.pkg);
    const names = [];
    keys.forEach(key => {
      const dep = State.deps.find(d => d.key === key);
      (dep && dep.pip || []).forEach(name => { if (names.indexOf(name) < 0) names.push(name); });
    });
    if (!names.length) return '';
    const sample = State.deps.find(d => d.install_command);
    const prefix = sample ? String(sample.install_command).split(' -m pip install ')[0] : 'py';
    return prefix + ' -m pip install ' + names.join(' ');
  }

  async function copyCommand(area, button) {
    const command = area ? area.value : '';
    if (!command) { VCS.toast('请先勾选需要安装的组件'); return; }
    let copied = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(command);
        copied = true;
      }
    } catch (_) { copied = false; }
    if (!copied && area) {
      area.focus(); area.select();
      try { copied = document.execCommand('copy'); } catch (_) { copied = false; }
    }
    if (button) {
      const old = button.textContent;
      button.textContent = copied ? '已复制' : '已选中，请 Ctrl+C';
      setTimeout(() => { button.textContent = old; }, 1800);
    }
    if (copied) VCS.toast('安装命令已复制');
  }

  // ── 依赖安装命令弹窗 ──
  function openInstallModal() {
    const installable = State.deps.filter(d => d.install_command);
    if (!installable.length) { VCS.toast('没有可用的 Python 依赖命令'); return; }
    const box = document.createElement('div');
    box.className = 'deps-modal';
    const intro = document.createElement('div');
    intro.className = State.runtimeInstallSupported ? 'sub' : 'warn-banner';
    intro.textContent = State.runtimeInstallSupported
      ? '三步即可：1. 复制推荐命令；2. 在 PowerShell / 终端粘贴并回车；3. 重启软件后点“重新检测”。'
      : (State.installNote || '当前是单文件 EXE，运行时无法把新包装入 EXE。') +
        ' 下方命令仅用于源码版 Python 环境，不会改造当前 EXE。';
    box.appendChild(intro);
    const installed = installable.filter(d => d.available).length;
    const status = document.createElement('div');
    status.className = 'deps-command-status';
    status.textContent = `Python 组件已安装 ${installed}/${installable.length}；` +
      '默认不选体积很大的 DECIMER。';
    box.appendChild(status);
    const matplotlib = State.deps.find(d => d.key === 'matplotlib');
    const readiness = document.createElement('div');
    readiness.className = 'deps-readiness';
    readiness.innerHTML = '<b>吸附能核心流程</b><span>结果导入、提交、续算、下载与 ΔE 不依赖 RDKit / DECIMER。</span>' +
      `<b>图表与完整报告</b><span>${matplotlib && matplotlib.available
        ? 'Matplotlib 已就绪。' : '缺少 Matplotlib；基础 ΔE 仍可用，但图表/报告会受限。'}</span>`;
    box.appendChild(readiness);
    if (!State.runtimeInstallSupported) {
      const frozenHelp = document.createElement('div');
      frozenHelp.className = 'deps-frozen-help';
      frozenHelp.innerHTML = '<b>当前是打包版软件，不能在运行中安装 Python 包。</b>' +
        '<span>请换用包含依赖的完整版安装包，或让维护者重新构建完整版。这里不会执行 pip，也不会再次打开当前 EXE。</span>' +
        '<span>RDKit 只影响分子编辑，DECIMER 只影响图片识别；不使用对应功能时无需安装。</span>';
      box.appendChild(frozenHelp);
      const modal = VCS.modal({
        title: '依赖说明', body: box,
        actions: [
          { label: '关闭', quiet: true, onClick: h => h.close() },
          { label: '重新检测', primary: true, onClick: async h => {
            await loadDeps(); h.close(); openInstallModal();
          } },
        ],
      });
      modal.el.classList.add('modal-wide');
      return;
    }
    const opts = document.createElement('div');
    opts.style.marginTop = '10px';
    opts.innerHTML = installable.map(d =>
      `<label class="deps-opt"><input type="checkbox" data-pkg="${VCS.esc(d.key)}" ` +
      `${(!d.available && RECOMMENDED.has(d.key)) ? 'checked' : ''}>` +
      `<span><b>${VCS.esc(d.name)}${d.available ? '(已装)' : ''}</b><span>${VCS.esc(d.note || '')}</span></span></label>`).join('') +
      '<div class="sub" style="margin-top:6px;color:var(--muted)">DECIMER 体积很大，只在需要图片识别化学式时勾选。</div>';
    box.appendChild(opts);
    const quick = document.createElement('div');
    quick.className = 'deps-command-quick';
    quick.innerHTML = '<button class="btn" type="button" data-pick="recommended">选择缺失的推荐组件</button>' +
      '<button class="btn quiet" type="button" data-pick="all">选择全部缺失组件</button>' +
      '<button class="btn quiet" type="button" data-pick="none">清除选择</button>';
    box.appendChild(quick);
    const label = document.createElement('div');
    label.className = 'meth-h';
    label.textContent = '在 PowerShell / 终端执行';
    box.appendChild(label);
    const command = document.createElement('textarea');
    command.className = 'ipt deps-log';
    command.id = 'deps-command';
    command.readOnly = true;
    command.rows = 3;
    box.appendChild(command);
    const update = () => { command.value = selectedCommand(box); };
    opts.querySelectorAll('input[data-pkg]').forEach(cb => cb.addEventListener('change', update));
    quick.querySelectorAll('[data-pick]').forEach(btn => btn.addEventListener('click', () => {
      opts.querySelectorAll('input[data-pkg]').forEach(cb => {
        const dep = State.deps.find(d => d.key === cb.dataset.pkg);
        cb.checked = btn.dataset.pick === 'all'
          ? !!(dep && !dep.available)
          : btn.dataset.pick === 'recommended'
            ? !!(dep && !dep.available && RECOMMENDED.has(dep.key)) : false;
      });
      update();
    }));
    update();
    let copyButton = null;
    const m = VCS.modal({
      title: '依赖安装命令', body: box,
      actions: [
        { label: '关闭', quiet: true, onClick: h => h.close() },
        { label: '重新检测', quiet: true, onClick: async h => {
          await loadDeps(); h.close(); openInstallModal();
        } },
        { label: '复制所选命令', primary: true,
          onClick: () => copyCommand(command, copyButton) },
      ],
    });
    m.el.classList.add('modal-wide');
    // VCS.modal 回调只传 handle，从弹窗尾部取主按钮做复制反馈。
    const buttons = m.el.querySelectorAll('button');
    copyButton = buttons[buttons.length - 1] || null;
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
