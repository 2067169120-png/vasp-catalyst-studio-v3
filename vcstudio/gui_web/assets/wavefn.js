// wavefn.js — ⑤波函数分析页(外部工具 / 输入与分析项 / 可视化)三 tab。
// tab1 外部工具:Multiwfn/VMD/GaussView/本地Gaussian 路径 + 检测(wavefn_probe)+ 记忆(tool_paths_*)。
// tab2 分析:波函数文件 + 七件套 chips + 本机/远程(实验性)+ 运行(wavefn_run / wavefn_run_remote),
//     Multiwfn 缺失时给可复制 stdin 脚本;产物 cube 汇总供 tab3 一键带入。
// tab3 可视化:场景 chips + 参数 + 渲染(wavefn_render;VMD 缺给 tcl)+ 查询极值(wavefn_extrema)。
// 只依赖 app.js 的 VCS.*;插值走 VCS.esc(SVG/脚本用 textContent/受信来源)。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const State = { analyses: [], scenes: [], sel: new Set(), scene: null, outputs: [] };

  function dirOf(p) {
    const s = String(p).replace(/[\\/]+$/, '');
    const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    return i > 0 ? s.slice(0, i) : '';
  }

  // ── tab 切换 ──
  function showTab(key) {
    document.querySelectorAll('#wf-tabs .wf-tab').forEach(b =>
      b.classList.toggle('on', b.dataset.wftab === key));
    document.querySelectorAll('#page-wavefunction .wf-pane').forEach(p =>
      p.hidden = p.dataset.wfpane !== key);
  }

  // ── tab1:外部工具路径 + 检测 ──
  async function loadToolPaths() {
    const r = await VCS.call('tool_paths_get');
    const paths = (r && r.paths) || {};
    document.querySelectorAll('.wf-tool-path').forEach(inp => {
      inp.value = paths[inp.dataset.tk] || '';
    });
  }
  function toolPathsFromForm() {
    const kv = {};
    document.querySelectorAll('.wf-tool-path').forEach(inp => { kv[inp.dataset.tk] = inp.value.trim(); });
    return kv;
  }
  async function saveToolPaths() {
    const r = await VCS.call('tool_paths_set', toolPathsFromForm());
    if (r && r.ok) { VCS.log('已保存外部工具路径', 'okc'); VCS.toast('已保存'); }
    else VCS.log('保存工具路径失败:' + ((r && r.error) || '未知'), 'failc');
  }
  async function probeTools() {
    await saveToolPaths();
    const r = await VCS.call('wavefn_probe', ['multiwfn', 'vmd', 'gaussview', 'gaussian']);
    if (!r || r.ok === false) { VCS.log('检测失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    Object.keys(r.tools || {}).forEach(k => {
      const el = document.querySelector(`[data-ts="${k}"]`);
      const t = r.tools[k];
      if (el) {
        el.textContent = t.available ? '✓ ' + (t.path || '可用') : '未找到';
        el.className = 'wf-tool-state ' + (t.available ? 'ok' : 'bad');
        el.title = t.detail || '';
      }
    });
    VCS.log('外部工具检测完成', 'okc');
  }

  // ── tab2:波函数文件 + 分析项 ──
  async function loadCatalog() {
    const r = await VCS.call('wavefn_scenes');
    State.analyses = (r && r.analyses) || [];
    State.scenes = (r && r.scenes) || [];
    renderAnalysisChips();
    renderSceneChips();
  }
  function renderAnalysisChips() {
    const box = $('wf-analysis-chips');
    if (!box) return;
    box.innerHTML = '';
    State.analyses.forEach(a => {
      const c = document.createElement('span');
      c.className = 'chip' + (State.sel.has(a.key) ? ' on' : '');
      c.dataset.val = a.key;
      c.textContent = a.name;
      c.title = a.note || '';
      c.addEventListener('click', () => {
        if (State.sel.has(a.key)) State.sel.delete(a.key); else State.sel.add(a.key);
        c.classList.toggle('on');
      });
      box.appendChild(c);
    });
  }
  function analysisParams() {
    const p = {};
    const orb = ($('wf-orbital') && $('wf-orbital').value.trim()) || '';
    if (orb) p.orbital = /^\d+$/.test(orb) ? parseInt(orb, 10) : orb;
    const frag = ($('wf-fragments') && $('wf-fragments').value.trim()) || '';
    if (frag) p.fragments = frag.split(',').map(s => s.trim()).filter(Boolean);
    return p;
  }
  function currentLocation() {
    const r = document.querySelector('input[name="wf-loc"]:checked');
    return r ? r.value : 'local';
  }
  function onLocationChange() {
    const remote = currentLocation() === 'remote';
    ['wf-remote-cluster', 'wf-remote-dir', 'wf-remote-exe'].forEach(id => {
      const el = $(id); if (el) el.hidden = !remote;
    });
    if (remote) populateRemoteClusters();
  }
  async function populateRemoteClusters() {
    const sel = $('wf-remote-cluster');
    if (!sel || sel.options.length) return;
    const r = await VCS.call('list_profiles');
    const profs = (r && r.profiles) || [];
    sel.innerHTML = profs.length
      ? profs.map(p => `<option value="${VCS.esc(p.name)}">${VCS.esc(p.name)}</option>`).join('')
      : '<option value="">(未配置集群)</option>';
  }
  async function runAnalysis() {
    const wf = ($('wf-file') && $('wf-file').value.trim()) || '';
    if (!wf) { VCS.log('请先选择波函数文件', 'failc'); return; }
    const keys = Array.from(State.sel);
    if (!keys.length) { VCS.log('请至少选择一个分析项', 'failc'); return; }
    const btn = $('wf-run');
    if (btn) btn.disabled = true;
    try {
      let r;
      if (currentLocation() === 'remote') {
        const name = ($('wf-remote-cluster') && $('wf-remote-cluster').value) || '';
        const rdir = ($('wf-remote-dir') && $('wf-remote-dir').value.trim()) || '';
        const rexe = ($('wf-remote-exe') && $('wf-remote-exe').value.trim()) || 'Multiwfn';
        if (!name) { VCS.log('远程分析:请选集群', 'failc'); return; }
        VCS.log('远程集群跑波函数分析(实验性):' + keys.join('、') + ' …');
        r = await VCS.call('wavefn_run_remote', wf, keys, name, null, rdir, rexe, false, analysisParams());
      } else {
        VCS.log('本机跑波函数分析:' + keys.join('、') + ' …');
        r = await VCS.call('wavefn_run', wf, keys, analysisParams(), null, null);
      }
      if (!r || r.error) { VCS.log('波函数分析失败:' + ((r && r.error) || '未知错误'), 'failc'); return; }
      renderResults(r.results || [], !!r.experimental);
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  function renderResults(results, experimental) {
    const box = $('wf-results');
    State.outputs = [];
    if (!box) return;
    if (!results.length) { box.innerHTML = '<span class="sub">无结果</span>'; return; }
    let h = experimental ? '<div class="wf-exp sub">远程分析为实验性能力</div>' : '';
    results.forEach(res => {
      const cls = res.ok ? 'okc' : 'failc';
      const outs = (res.outputs || []);
      outs.forEach(o => { if (State.outputs.indexOf(o) < 0) State.outputs.push(o); });
      h += `<div class="wf-res"><div class="wf-res-h ${cls}">` +
        `${VCS.esc(res.analysis)} — ${res.ok ? '完成' : '失败'}` +
        (res.elapsed_s ? ` (${res.elapsed_s}s)` : '') + '</div>';
      if (res.error) h += `<div class="sub">${VCS.esc(res.error)}</div>`;
      if (outs.length) h += `<div class="sub">产物:${outs.map(o => VCS.esc(o)).join('、')}</div>`;
      if (res.extrema) {
        const mn = (res.extrema.minima || []).length, mx = (res.extrema.maxima || []).length;
        h += `<div class="sub">表面极值:极小 ${mn} 个 / 极大 ${mx} 个(见「可视化」查询极值)</div>`;
      }
      if (res.script) {
        h += '<div class="sub">Multiwfn 未就绪 — 可复制以下 stdin 脚本手动运行:</div>' +
          `<textarea class="ipt wf-script" readonly rows="4"></textarea>`;
      }
      if (res.stdout_tail) h += `<pre class="mono wf-stdout"></pre>`;
      h += '</div>';
    });
    box.innerHTML = h;
    // textContent 注入脚本/stdout(避免把脚本内容当 HTML)
    let si = 0, oi = 0;
    results.forEach(res => {
      if (res.script) { const t = box.querySelectorAll('.wf-script')[si++]; if (t) t.value = res.script; }
      if (res.stdout_tail) { const p = box.querySelectorAll('.wf-stdout')[oi++]; if (p) p.textContent = res.stdout_tail; }
    });
    renderFromOutputs();
  }
  function renderFromOutputs() {
    const box = $('wf-fromoutputs');
    if (!box) return;
    if (!State.outputs.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<span class="sub">分析产物(点击带入可视化):</span> ' +
      State.outputs.map(o => `<span class="chip out" data-out="${VCS.esc(o)}">${VCS.esc(o.split(/[\\/]/).pop())}</span>`).join('');
  }

  // ── tab3:可视化 ──
  function renderSceneChips() {
    const box = $('wf-scene-chips');
    if (!box) return;
    box.innerHTML = '';
    State.scenes.forEach(s => {
      const c = document.createElement('span');
      c.className = 'chip' + (State.scene === s.key ? ' on' : '');
      c.dataset.val = s.key;
      c.textContent = s.name;
      c.title = s.note || '';
      c.addEventListener('click', () => selectScene(s.key));
      box.appendChild(c);
    });
  }
  function selectScene(key) {
    State.scene = key;
    document.querySelectorAll('#wf-scene-chips .chip').forEach(
      c => c.classList.toggle('on', c.dataset.val === key));
    renderSceneFiles();
  }
  function sceneRoles() {
    const s = State.scenes.find(x => x.key === State.scene);
    return (s && s.files) || [];
  }
  function renderSceneFiles() {
    let host = $('wf-scene-files');
    if (!host) {
      host = document.createElement('div');
      host.id = 'wf-scene-files';
      host.className = 'wf-scene-files';
      const chips = $('wf-scene-chips');
      if (chips && chips.parentNode) chips.parentNode.appendChild(host);
    }
    const roles = sceneRoles();
    if (!roles.length) { host.innerHTML = '<span class="sub">选择一个场景后指定所需 cube/结构文件</span>'; return; }
    host.innerHTML = roles.map(r =>
      `<div class="gen-inrow" style="margin-top:4px"><span class="wf-role">${VCS.esc(r)}</span>` +
      `<input class="ipt wf-role-in" data-role="${VCS.esc(r)}" placeholder="${VCS.esc(r)} 文件路径" style="flex:1">` +
      `<button class="btn wf-role-browse" data-role="${VCS.esc(r)}">浏览</button></div>`).join('');
  }
  function sceneFiles() {
    const files = {};
    document.querySelectorAll('#wf-scene-files .wf-role-in').forEach(inp => {
      if (inp.value.trim()) files[inp.dataset.role] = inp.value.trim();
    });
    // 单文件场景(仅 wf-vis-file):role 也接受主文件
    const roles = sceneRoles();
    const main = ($('wf-vis-file') && $('wf-vis-file').value.trim()) || '';
    if (roles.length === 1 && !files[roles[0]] && main) files[roles[0]] = main;
    return files;
  }
  function renderParams() {
    const iso = parseFloat(($('wf-iso') && $('wf-iso').value) || '0.001') || 0.001;
    const orbiso = parseFloat(($('wf-orbiso') && $('wf-orbiso').value) || '0.05') || 0.05;
    const size = parseFloat(($('wf-extsize') && $('wf-extsize').value) || '0.1') || 0.1;
    const p = { iso: iso, extrema_size: size };
    if (State.scene === 'orbital') p.iso = orbiso;   // 轨道用 orbiso
    return p;
  }
  async function render() {
    if (!State.scene) { VCS.log('请先选择渲染场景', 'failc'); return; }
    const files = sceneFiles();
    const roles = sceneRoles();
    for (const r of roles) if (!files[r]) { VCS.log('渲染:请指定「' + r + '」文件', 'failc'); return; }
    const first = files[roles[0]] || ($('wf-vis-file') && $('wf-vis-file').value.trim()) || '';
    const d = dirOf(first) || dirOf(($('wf-file') && $('wf-file').value.trim()) || '');
    const sep = first.indexOf('\\') >= 0 ? '\\' : '/';
    const outPng = (d ? d + sep : '') + State.scene + '.png';
    const btn = $('wf-render');
    if (btn) btn.disabled = true;
    VCS.log('VMD 渲染场景 ' + State.scene + ' …');
    try {
      const r = await VCS.call('wavefn_render', State.scene, files, outPng, renderParams(), null);
      const out = $('wf-render-out');
      if (!r || r.ok === false || r.error) {
        VCS.log('渲染失败:' + ((r && r.error) || '未知错误'), 'failc');
        if (r && r.tcl && out) {
          out.innerHTML = '<div class="sub">VMD 未就绪 — 可复制以下 Tcl 手动渲染:</div><textarea class="ipt" readonly rows="6" id="wf-tcl"></textarea>';
          const t = $('wf-tcl'); if (t) t.value = r.tcl;
        }
        return;
      }
      VCS.log('渲染完成:' + r.png, 'okc');
      if (out) {
        out.innerHTML = `<img class="wf-png" src="file://${VCS.esc(r.png)}" alt="render">` +
          `<div class="actions" style="margin-top:6px"><button class="btn" id="wf-open-png">打开目录</button></div>`;
        const ob = $('wf-open-png');
        if (ob) ob.addEventListener('click', () => VCS.call('open_dir', r.png));
      }
      VCS.toast('渲染完成');
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  async function queryExtrema() {
    const wf = ($('wf-file') && $('wf-file').value.trim()) || '';
    if (!wf) { VCS.log('查询极值:请在「输入与分析」选波函数文件', 'failc'); return; }
    VCS.log('查询表面极值(Multiwfn)…');
    const r = await VCS.call('wavefn_extrema', wf, 'esp_extrema', null, null);
    const out = $('wf-extrema-out');
    if (!r || r.ok === false || r.error) {
      VCS.log('查询极值失败:' + ((r && r.error) || '未知错误'), 'failc');
      if (r && r.script && out) {
        out.innerHTML = '<div class="sub">Multiwfn 未就绪 — 可复制脚本手动运行:</div><textarea class="ipt" readonly rows="4" id="wf-ext-script"></textarea>';
        const t = $('wf-ext-script'); if (t) t.value = r.script;
      }
      return;
    }
    const fmt = arr => (arr || []).map(p =>
      `${p.value_kcal} @ (${p.xyz.map(v => (+v).toFixed(3)).join(', ')})`).join('\n');
    const text = '极小点(亲电位点):\n' + (fmt(r.minima) || '(无)') +
      '\n\n极大点:\n' + (fmt(r.maxima) || '(无)');
    if (out) {
      out.innerHTML = '<div class="wf-res-h okc">表面极值(min/max)</div>' +
        '<pre class="mono" id="wf-ext-list"></pre>' +
        '<button class="btn" id="wf-ext-copy">复制</button>';
      const pre = $('wf-ext-list'); if (pre) pre.textContent = text;
      const cb = $('wf-ext-copy');
      if (cb) cb.addEventListener('click', async () => {
        try {
          if (navigator.clipboard) await navigator.clipboard.writeText(text);
        } catch (e) { /* 忽略 */ }
        VCS.toast('已复制极值');
      });
    }
    VCS.log('极值查询完成:极小 ' + (r.minima || []).length + ' / 极大 ' + (r.maxima || []).length, 'okc');
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    document.querySelectorAll('#wf-tabs .wf-tab').forEach(b =>
      b.addEventListener('click', () => showTab(b.dataset.wftab)));
    document.querySelectorAll('.wf-tool-browse').forEach(b => b.addEventListener('click', async () => {
      const r = await VCS.call('pick_file', 'exe');
      if (r && r.path) { const inp = document.querySelector(`.wf-tool-path[data-tk="${b.dataset.tk}"]`); if (inp) inp.value = r.path; }
    }));
    wire('wf-probe', probeTools);
    wire('wf-tools-save', saveToolPaths);
    wire('wf-file-btn', async () => {
      const r = await VCS.call('pick_file', 'wavefn');
      if (r && r.path && $('wf-file')) $('wf-file').value = r.path;
    });
    wire('wf-run', runAnalysis);
    document.querySelectorAll('input[name="wf-loc"]').forEach(r => r.addEventListener('change', onLocationChange));
    wire('wf-vis-btn', async () => {
      const r = await VCS.call('pick_file', 'cube');
      if (r && r.path && $('wf-vis-file')) $('wf-vis-file').value = r.path;
    });
    wire('wf-render', render);
    wire('wf-extrema', queryExtrema);
    // 产物带入可视化
    const fo = $('wf-fromoutputs');
    if (fo) fo.addEventListener('click', e => {
      const c = e.target.closest('[data-out]');
      if (!c) return;
      if ($('wf-vis-file')) $('wf-vis-file').value = c.dataset.out;
      const roles = sceneRoles();
      const empty = document.querySelector('#wf-scene-files .wf-role-in:not([data-filled])');
      if (roles.length && empty) { empty.value = c.dataset.out; empty.setAttribute('data-filled', '1'); }
      VCS.toast('已带入可视化文件');
    });
    // 场景角色文件浏览(委托)
    const sf = document.querySelector('#page-wavefunction');
    if (sf) sf.addEventListener('click', async e => {
      const b = e.target.closest('.wf-role-browse');
      if (!b) return;
      const r = await VCS.call('pick_file', 'cube');
      if (r && r.path) {
        const inp = document.querySelector(`.wf-role-in[data-role="${b.dataset.role}"]`);
        if (inp) inp.value = r.path;
      }
    });
    loadToolPaths();
    loadCatalog();
    onLocationChange();
  }

  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'wavefunction') { init(); loadToolPaths(); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Wavefn = { reload: loadCatalog };
})();
