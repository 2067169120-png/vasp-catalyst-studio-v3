// wavefn.js — ⑤波函数分析页(外部工具 / 输入与分析项 / 可视化)三 tab。
// tab1 外部工具:Multiwfn/VMD/GaussView/本地Gaussian 路径 + 检测(wavefn_probe)+ 记忆(tool_paths_*)。
// tab2 分析:波函数文件 + 七件套 chips + 本机/远程(实验性)+ 运行(wavefn_run / wavefn_run_remote),
//     Multiwfn 缺失时给可复制 stdin 脚本;产物 cube 汇总供 tab3 一键带入。
// tab3 可视化:场景 chips + 参数 + 渲染(wavefn_render;VMD 缺给 tcl)+ 查询极值(wavefn_extrema)。
// 只依赖 app.js 的 VCS.*;插值走 VCS.esc(SVG/脚本用 textContent/受信来源)。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  function tr(key, params = {}, zhFallback = '', enFallback = '') {
    const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
    const fallback = english
      ? (enFallback || `Translation unavailable (${key})`)
      : zhFallback;
    if (typeof VCS.t === 'function') return VCS.t(key, params, fallback);
    return String(fallback).replace(/\{([A-Za-z0-9_]+)\}/g,
      (match, name) => Object.prototype.hasOwnProperty.call(params, name)
        ? String(params[name]) : match);
  }
  const State = { analyses: [], groups: [], apiKeys: new Set(), scenes: [],
    sel: new Set(), scene: null, outputs: [] };

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
    if (r && r.ok) {
      VCS.log(tr('runtime.wavefn.tools.saved_paths', {},
        '已保存外部工具路径', 'External-tool paths saved'), 'okc');
      VCS.toast(tr('runtime.wavefn.common.saved', {}, '已保存', 'Saved'));
    } else VCS.log(tr('runtime.wavefn.tools.save_failed', {
      error: (r && r.error) || tr('runtime.wavefn.common.unknown', {}, '未知', 'Unknown'),
    }, '保存工具路径失败:{error}', 'Failed to save tool paths: {error}'), 'failc');
  }
  async function probeTools() {
    await saveToolPaths();
    const r = await VCS.call('wavefn_probe', ['multiwfn', 'vmd', 'gaussview', 'gaussian']);
    if (!r || r.ok === false) {
      VCS.log(tr('runtime.wavefn.tools.probe_failed', {
        error: (r && r.error) || tr('runtime.wavefn.common.unknown', {}, '未知', 'Unknown'),
      }, '检测失败:{error}', 'Detection failed: {error}'), 'failc'); return;
    }
    Object.keys(r.tools || {}).forEach(k => {
      const el = document.querySelector(`[data-ts="${k}"]`);
      const t = r.tools[k];
      if (el) {
        el.textContent = t.available ? '✓ ' + (t.path || tr(
          'runtime.wavefn.tools.available', {}, '可用', 'Available')) : tr(
          'runtime.wavefn.tools.not_found', {}, '未找到', 'Not found');
        el.className = 'wf-tool-state ' + (t.available ? 'ok' : 'bad');
        el.title = t.detail || '';
      }
    });
    VCS.log(tr('runtime.wavefn.tools.probe_complete', {},
      '外部工具检测完成', 'External-tool detection complete'), 'okc');
  }

  // ── tab2:波函数文件 + 分析项 ──
  async function loadCatalog() {
    const r = await VCS.call('wavefn_scenes');
    State.analyses = (r && r.analyses) || [];
    State.scenes = (r && r.scenes) || [];
    const g = await VCS.call('wavefn_analyses');       // 分组菜单(单一事实源=引擎注册表;source=api 仅旧后端出现)
    State.groups = (g && g.groups) || [];
    State.apiKeys = new Set();
    State.groups.forEach(grp => (grp.items || []).forEach(it => {
      if (it.source === 'api') State.apiKeys.add(it.key);
    }));
    renderAnalysisChips();
    renderSceneChips();
  }
  // 分析项 chips 墙 → 分组多选下拉(常用 / 实空间与截面 / 弱相互作用 / 其它)。
  // 保留 wf-analysis-chips 容器 id;State.sel 仍为选中项真源(被 wavefn_run 消费)。
  function applyPlaneRow() {
    const plane = $('wf-plane-row');
    if (plane) plane.hidden = !State.sel.has('elf_lol_section');
  }
  function renderAnalysisChips() {
    const box = $('wf-analysis-chips');
    if (!box) return;
    const src = State.groups.length ? State.groups : [{ group: '', items: State.analyses }];
    const groups = src.map(grp => ({
      group: grp.group || '',
      items: (grp.items || []).map(a => ({
        val: a.key, label: a.name, note: a.note || '', exp: a.source === 'api',
      })),
    }));
    if (window.VCS && VCS.ui && VCS.ui.multiselect) {
      if (box._ms) {
        box._ms.setGroups(groups, true);
        box._ms.setSelected(Array.from(State.sel));
      } else {
        VCS.ui.multiselect(box, {
          groups: groups, selected: Array.from(State.sel), placeholder: tr(
            'runtime.wavefn.analysis.multiselect', {},
            '选择分析项(可多选)', 'Select one or more analyses'),
          onChange: arr => { State.sel = new Set(arr); applyPlaneRow(); },
        });
      }
      applyPlaneRow();
    } else {                                   // 兜底:原分组 chips
      box.innerHTML = src.map(grp =>
        `<div class="wf-agroup">${grp.group ? `<div class="wf-agroup-h">${VCS.esc(grp.group)}</div>` : ''}` +
        `<div class="chips">` + (grp.items || []).map(a =>
          `<span class="chip${State.sel.has(a.key) ? ' on' : ''}${a.source === 'api' ? ' api-extra' : ''}" ` +
          `data-val="${VCS.esc(a.key)}" title="${VCS.esc(a.note || '')}">${VCS.esc(a.name)}</span>`).join('') +
        `</div></div>`).join('');
      box.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
        const k = c.dataset.val;
        if (State.sel.has(k)) State.sel.delete(k); else State.sel.add(k);
        c.classList.toggle('on');
        applyPlaneRow();
      }));
    }
  }
  function analysisParams() {
    const p = {};
    const orb = ($('wf-orbital') && $('wf-orbital').value.trim()) || '';
    if (orb) p.orbital = /^\d+$/.test(orb) ? parseInt(orb, 10) : orb;
    const frag = ($('wf-fragments') && $('wf-fragments').value.trim()) || '';
    if (frag) p.fragments = frag.split(',').map(s => s.trim()).filter(Boolean);
    const grid = ($('wf-grid') && $('wf-grid').value) || '';
    if (grid) p.grid = parseInt(grid, 10);            // 格点精度透传 1/2/3
    const plane = ($('wf-plane') && $('wf-plane').value) || '';
    if (plane) p.plane = plane;
    const atoms = ($('wf-plane-atoms') && $('wf-plane-atoms').value.trim()) || '';
    if (atoms) p.atoms = atoms.split(/\s+/).map(Number).filter(n => n > 0);
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
      : `<option value="">${VCS.esc(tr('runtime.wavefn.cluster.none_configured', {},
        '(未配置集群)', '(No cluster configured)'))}</option>`;
  }
  async function runAnalysis() {
    const wf = ($('wf-file') && $('wf-file').value.trim()) || '';
    if (!wf) {
      VCS.log(tr('runtime.wavefn.analysis.file_required', {},
        '请先选择波函数文件', 'Select a wavefunction file first'), 'failc'); return;
    }
    const keys = Array.from(State.sel);
    if (!keys.length) {
      VCS.log(tr('runtime.wavefn.analysis.selection_required', {},
        '请至少选择一个分析项', 'Select at least one analysis'), 'failc'); return;
    }
    const btn = $('wf-run');
    if (btn) btn.disabled = true;
    try {
      let r;
      if (currentLocation() === 'remote') {
        const name = ($('wf-remote-cluster') && $('wf-remote-cluster').value) || '';
        const rdir = ($('wf-remote-dir') && $('wf-remote-dir').value.trim()) || '';
        const rexe = ($('wf-remote-exe') && $('wf-remote-exe').value.trim()) || 'Multiwfn';
        if (!name) {
          VCS.log(tr('runtime.wavefn.analysis.remote_cluster_required', {},
            '远程分析:请选集群', 'Remote analysis: select a cluster'), 'failc'); return;
        }
        VCS.log(tr('runtime.wavefn.analysis.remote_running', { analyses: keys.join(', ') },
          '远程集群跑波函数分析(实验性):{analyses} …',
          'Running experimental wavefunction analysis on a remote cluster: {analyses} …'));
        r = await VCS.call('wavefn_run_remote', wf, keys, name, null, rdir, rexe, false, analysisParams());
      } else {
        // v3.2.2 起四补充项已入引擎注册表(source=engine)统一走 wavefn_run;
        // apiKeys 路由仅为旧后端(source=api)保留,新后端该集合为空。
        const engKeys = keys.filter(k => !State.apiKeys.has(k));
        const apiKeys = keys.filter(k => State.apiKeys.has(k));
        VCS.log(tr('runtime.wavefn.analysis.local_running', { analyses: keys.join(', ') },
          '本机跑波函数分析:{analyses} …',
          'Running wavefunction analysis locally: {analyses} …'));
        let results = [];
        if (engKeys.length) {
          const re = await VCS.call('wavefn_run', wf, engKeys, analysisParams(), null, null);
          if (re && !re.error) results = results.concat(re.results || []);
        }
        if (apiKeys.length) {
          const ra = await VCS.call('wavefn_run_extra', wf, apiKeys, analysisParams(), null, null);
          if (ra && !ra.error) results = results.concat(ra.results || []);
        }
        r = { results: results, experimental: false };
      }
      if (!r || r.error) {
        VCS.log(tr('runtime.wavefn.analysis.failed', {
          error: (r && r.error) || tr(
            'runtime.wavefn.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '波函数分析失败:{error}', 'Wavefunction analysis failed: {error}'), 'failc'); return;
      }
      renderResults(r.results || [], !!r.experimental);
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  // 降级归因(P1-2):区分「引擎缺 run_script 方法」(现已修复)与「Multiwfn 未安装」(probe 失败)。
  // 手动运行 stdin 脚本的提示只在真缺 Multiwfn 时显示;引擎缺方法只提示更新程序,不误导为未装 Multiwfn。
  function engineMissing(res) {
    return !!(res && res.error && /引擎待扩展|run_script/.test(res.error));
  }
  function showManualScript(res) {
    return !!(res && res.script) && !engineMissing(res);
  }
  function renderResults(results, experimental) {
    const box = $('wf-results');
    State.outputs = [];
    if (!box) return;
    if (!results.length) {
      box.innerHTML = `<span class="sub">${VCS.esc(tr(
        'runtime.wavefn.results.empty', {}, '无结果', 'No results',
      ))}</span>`;
      return;
    }
    let h = experimental ? `<div class="wf-exp sub">${VCS.esc(tr(
      'runtime.wavefn.results.remote_experimental', {},
      '远程分析为实验性能力', 'Remote analysis is experimental',
    ))}</div>` : '';
    results.forEach(res => {
      const cls = res.ok ? 'okc' : 'failc';
      const outs = (res.outputs || []);
      outs.forEach(o => { if (State.outputs.indexOf(o) < 0) State.outputs.push(o); });
      h += `<div class="wf-res"><div class="wf-res-h ${cls}">` +
        `${VCS.esc(res.analysis)} — ${VCS.esc(res.ok
          ? tr('runtime.wavefn.results.complete', {}, '完成', 'Complete')
          : tr('runtime.wavefn.results.failed', {}, '失败', 'Failed'))}` +
        (res.elapsed_s ? ` (${res.elapsed_s}s)` : '') + '</div>';
      if (res.error) h += `<div class="sub">${VCS.esc(res.error)}</div>`;
      if (outs.length) h += `<div class="sub">${VCS.esc(tr(
        'runtime.wavefn.results.outputs', { outputs: outs.map(o => VCS.esc(o)).join(', ') },
        '产物:{outputs}', 'Outputs: {outputs}',
      ))}</div>`;
      if (res.extrema) {
        const mn = (res.extrema.minima || []).length, mx = (res.extrema.maxima || []).length;
        h += `<div class="sub">${VCS.esc(tr('runtime.wavefn.results.extrema', {
          minima: mn, maxima: mx,
        }, '表面极值:极小 {minima} 个 / 极大 {maxima} 个(见「可视化」查询极值)',
        'Surface extrema: {minima} minima / {maxima} maxima (query extrema under Visualization)'))}</div>`;
      }
      if (showManualScript(res)) {
        h += `<div class="sub">${VCS.esc(tr(
          'runtime.wavefn.results.manual_script', {},
          'Multiwfn 未安装/未就绪(probe 失败)— 可复制以下 stdin 脚本手动运行:',
          'Multiwfn is not installed or ready (probe failed); copy the stdin script below to run manually:',
        ))}</div>` +
          `<textarea class="ipt wf-script" readonly rows="4"></textarea>`;
      } else if (engineMissing(res)) {
        h += `<div class="sub" style="color:var(--warn)">${VCS.esc(tr(
          'runtime.wavefn.results.engine_method_missing', {},
          '波函数引擎缺 run_script 方法(此版本应已接通;若仍出现请更新程序)',
          'The wavefunction engine lacks run_script; this version should provide it, so update the application if this persists',
        ))}</div>`;
      }
      if (res.stdout_tail) h += `<pre class="mono wf-stdout"></pre>`;
      h += '</div>';
    });
    box.innerHTML = h;
    // textContent 注入脚本/stdout(避免把脚本内容当 HTML);脚本仅在真缺 Multiwfn 时注入
    let si = 0, oi = 0;
    results.forEach(res => {
      if (showManualScript(res)) { const t = box.querySelectorAll('.wf-script')[si++]; if (t) t.value = res.script; }
      if (res.stdout_tail) { const p = box.querySelectorAll('.wf-stdout')[oi++]; if (p) p.textContent = res.stdout_tail; }
    });
    renderFromOutputs();
  }
  function renderFromOutputs() {
    const box = $('wf-fromoutputs');
    if (!box) return;
    if (!State.outputs.length) { box.innerHTML = ''; return; }
    box.innerHTML = `<span class="sub">${VCS.esc(tr(
      'runtime.wavefn.outputs.transfer_hint', {},
      '分析产物(点击带入可视化):',
      'Analysis outputs (select to transfer to Visualization):',
    ))}</span> ` +
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
  // 各场景 iso 默认值(等值面语义不同,数量级差很大;输入框留空即用场景默认)
  const SCENE_ISO = { esp_surface: 0.001, alie_surface: 0.001, nci: 0.5, iri: 1.0,
    igmh: 0.01, orbital: 0.05, fukui: 0.05 };
  function selectScene(key) {
    State.scene = key;
    document.querySelectorAll('#wf-scene-chips .chip').forEach(
      c => c.classList.toggle('on', c.dataset.val === key));
    const iso = $('wf-iso');
    if (iso && SCENE_ISO[key] != null) iso.placeholder = tr(
      'runtime.wavefn.scene.default_iso', { value: SCENE_ISO[key] },
      '默认 {value}', 'Default {value}');
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
    if (!roles.length) {
      host.innerHTML = `<span class="sub">${VCS.esc(tr(
        'runtime.wavefn.scene.select_hint', {},
        '选择一个场景后指定所需 cube/结构文件',
        'Select a scene, then specify the required cube/structure files',
      ))}</span>`;
      return;
    }
    host.innerHTML = roles.map(r =>
      `<div class="gen-inrow" style="margin-top:4px"><span class="wf-role">${VCS.esc(r)}</span>` +
      `<input class="ipt wf-role-in" data-role="${VCS.esc(r)}" placeholder="${VCS.esc(tr(
        'runtime.wavefn.scene.file_path', { role: r }, '{role} 文件路径', '{role} file path',
      ))}" style="flex:1">` +
      `<button class="btn wf-role-browse" data-role="${VCS.esc(r)}">${VCS.esc(tr(
        'runtime.wavefn.common.browse', {}, '浏览', 'Browse',
      ))}</button></div>`).join('');
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
    const isoRaw = ($('wf-iso') && $('wf-iso').value.trim()) || '';
    const orbRaw = ($('wf-orbiso') && $('wf-orbiso').value.trim()) || '';
    const size = parseFloat(($('wf-extsize') && $('wf-extsize').value) || '0.1') || 0.1;
    const p = { extrema_size: size };
    // iso 留空 → 不传参,交由各场景引擎默认值(esp 0.001 / nci 0.5 / iri 1.0 / igmh 0.01…)
    if (State.scene === 'orbital' || State.scene === 'fukui') {
      if (orbRaw) p.iso = parseFloat(orbRaw) || 0.05;   // 轨道族用 orbiso 输入
    } else if (isoRaw) {
      p.iso = parseFloat(isoRaw) || SCENE_ISO[State.scene] || 0.001;
    }
    return p;
  }
  async function render() {
    if (!State.scene) {
      VCS.log(tr('runtime.wavefn.render.scene_required', {},
        '请先选择渲染场景', 'Select a rendering scene first'), 'failc'); return;
    }
    const files = sceneFiles();
    const roles = sceneRoles();
    for (const r of roles) if (!files[r]) {
      VCS.log(tr('runtime.wavefn.render.file_required', { role: r },
        '渲染:请指定「{role}」文件', 'Rendering: specify the “{role}” file'), 'failc'); return;
    }
    const first = files[roles[0]] || ($('wf-vis-file') && $('wf-vis-file').value.trim()) || '';
    const d = dirOf(first) || dirOf(($('wf-file') && $('wf-file').value.trim()) || '');
    const sep = first.indexOf('\\') >= 0 ? '\\' : '/';
    const outPng = (d ? d + sep : '') + State.scene + '.png';
    const btn = $('wf-render');
    if (btn) btn.disabled = true;
    const server = (document.querySelector('input[name="wf-render-loc"]:checked') || {}).value === 'server';
    VCS.log(tr('runtime.wavefn.render.running', {
      location: server
        ? tr('runtime.wavefn.render.server_experimental', {},
          '服务器(实验性)', 'Server (experimental)')
        : tr('runtime.wavefn.render.local', {}, '本机', 'Local machine'),
      scene: State.scene,
    }, '{location} VMD 渲染场景 {scene} …', '{location} VMD rendering scene {scene} …'));
    try {
      let r;
      if (server) {
        const cl = ($('wf-render-cluster') && $('wf-render-cluster').value) || '';
        const rdir = ($('wf-render-dir') && $('wf-render-dir').value.trim()) || '';
        if (!cl) {
          VCS.log(tr('runtime.wavefn.render.cluster_required', {},
            '服务器渲染:请选集群', 'Server rendering: select a cluster'), 'failc'); return;
        }
        r = await VCS.call('wavefn_render_remote', State.scene, files, cl, null, rdir, 'vmd', false, renderParams());
      } else {
        r = await VCS.call('wavefn_render', State.scene, files, outPng, renderParams(), null);
      }
      const out = $('wf-render-out');
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.wavefn.render.failed', {
          error: (r && r.error) || tr(
            'runtime.wavefn.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '渲染失败:{error}', 'Rendering failed: {error}'), 'failc');
        if (r && r.tcl && out) {
          out.innerHTML = `<div class="sub">${VCS.esc(tr(
            'runtime.wavefn.render.manual_tcl', {},
            'VMD 未就绪 — 可复制以下 Tcl 手动渲染:',
            'VMD is not ready; copy the Tcl below to render manually:',
          ))}</div><textarea class="ipt" readonly rows="6" id="wf-tcl"></textarea>`;
          const t = $('wf-tcl'); if (t) t.value = r.tcl;
        }
        return;
      }
      VCS.log(tr('runtime.wavefn.render.complete_path', { path: r.png },
        '渲染完成:{path}', 'Rendering complete: {path}'), 'okc');
      if (out) {
        out.innerHTML = `<img class="wf-png" src="file://${VCS.esc(r.png)}" alt="render">` +
          `<div class="actions" style="margin-top:6px"><button class="btn" id="wf-open-png">${VCS.esc(tr(
            'runtime.wavefn.common.open_directory', {}, '打开目录', 'Open directory',
          ))}</button></div>`;
        const ob = $('wf-open-png');
        if (ob) ob.addEventListener('click', () => VCS.call('open_dir', r.png));
      }
      VCS.toast(tr('runtime.wavefn.render.complete', {}, '渲染完成', 'Rendering complete'));
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  async function queryExtrema() {
    const wf = ($('wf-file') && $('wf-file').value.trim()) || '';
    if (!wf) {
      VCS.log(tr('runtime.wavefn.extrema.file_required', {},
        '查询极值:请在「输入与分析」选波函数文件',
        'Extrema query: select a wavefunction file under Input and Analysis'), 'failc'); return;
    }
    VCS.log(tr('runtime.wavefn.extrema.querying', {},
      '查询表面极值(Multiwfn)…', 'Querying surface extrema with Multiwfn…'));
    const r = await VCS.call('wavefn_extrema', wf, 'esp_extrema', null, null);
    const out = $('wf-extrema-out');
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.wavefn.extrema.failed', {
        error: (r && r.error) || tr(
          'runtime.wavefn.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '查询极值失败:{error}', 'Extrema query failed: {error}'), 'failc');
      if (r && r.script && out) {
        out.innerHTML = `<div class="sub">${VCS.esc(tr(
          'runtime.wavefn.extrema.manual_script', {},
          'Multiwfn 未就绪 — 可复制脚本手动运行:',
          'Multiwfn is not ready; copy the script to run manually:',
        ))}</div><textarea class="ipt" readonly rows="4" id="wf-ext-script"></textarea>`;
        const t = $('wf-ext-script'); if (t) t.value = r.script;
      }
      return;
    }
    const fmt = arr => (arr || []).map(p =>
      `${p.value_kcal} @ (${p.xyz.map(v => (+v).toFixed(3)).join(', ')})`).join('\n');
    const text = tr('runtime.wavefn.extrema.text', {
      minima: fmt(r.minima) || tr('runtime.wavefn.common.none', {}, '(无)', '(None)'),
      maxima: fmt(r.maxima) || tr('runtime.wavefn.common.none', {}, '(无)', '(None)'),
    }, '极小点(亲电位点):\n{minima}\n\n极大点:\n{maxima}',
    'Minima (electrophilic sites):\n{minima}\n\nMaxima:\n{maxima}');
    if (out) {
      out.innerHTML = `<div class="wf-res-h okc">${VCS.esc(tr(
        'runtime.wavefn.extrema.heading', {},
        '表面极值(min/max)', 'Surface extrema (min/max)',
      ))}</div>` +
        '<pre class="mono" id="wf-ext-list"></pre>' +
        `<button class="btn" id="wf-ext-copy">${VCS.esc(tr(
          'runtime.wavefn.common.copy', {}, '复制', 'Copy',
        ))}</button>`;
      const pre = $('wf-ext-list'); if (pre) pre.textContent = text;
      const cb = $('wf-ext-copy');
      if (cb) cb.addEventListener('click', async () => {
        try {
          if (navigator.clipboard) await navigator.clipboard.writeText(text);
        } catch (e) { /* 忽略 */ }
        VCS.toast(tr('runtime.wavefn.extrema.copied', {},
          '已复制极值', 'Extrema copied'));
      });
    }
    VCS.log(tr('runtime.wavefn.extrema.complete', {
      minima: (r.minima || []).length, maxima: (r.maxima || []).length,
    }, '极值查询完成:极小 {minima} / 极大 {maxima}',
    'Extrema query complete: {minima} minima / {maxima} maxima'), 'okc');
  }

  // ── 渲染位置(本机/服务器)切换 ──
  function onRenderLocChange() {
    const server = (document.querySelector('input[name="wf-render-loc"]:checked') || {}).value === 'server';
    ['wf-render-cluster', 'wf-render-dir'].forEach(id => { const el = $(id); if (el) el.hidden = !server; });
    if (server) {
      const sel = $('wf-render-cluster');
      if (sel && !sel.options.length) VCS.call('list_profiles').then(r => {
        const profs = (r && r.profiles) || [];
        sel.innerHTML = profs.length
          ? profs.map(p => `<option value="${VCS.esc(p.name)}">${VCS.esc(p.name)}</option>`).join('')
          : `<option value="">${VCS.esc(tr('runtime.wavefn.cluster.none_configured', {},
            '(未配置集群)', '(No cluster configured)'))}</option>`;
      });
    }
  }

  // ── Fukui / CDFT 四描述符(走 wavefn_run_extra 的 fukui_cdft) ──
  async function runFukui(which) {
    const wf = ($('wf-file') && $('wf-file').value.trim()) || '';
    if (!wf) {
      VCS.log(tr('runtime.wavefn.fukui.file_required', {},
        'Fukui:请在「输入与分析」选波函数文件',
        'Fukui: select a wavefunction file under Input and Analysis'), 'failc'); return;
    }
    const out = $('wf-fukui-out');
    if (out) out.textContent = tr('runtime.wavefn.fukui.calculating', {},
      '计算 Fukui/CDFT(需 N-1/N/N+1 波函数)…',
      'Calculating Fukui/CDFT descriptors (requires N-1/N/N+1 wavefunctions)…');
    const r = await VCS.call('wavefn_run_extra', wf, ['fukui_cdft'], { which: which }, null, null);
    const res = (r && r.results && r.results[0]) || {};
    if (res.ok) {
      if (out) out.textContent = tr('runtime.wavefn.fukui.outputs', {
        outputs: (res.outputs || []).join(', '),
      }, '已产出 {outputs},可在上方渲染等值面',
      'Generated {outputs}; render the isosurface above');
      VCS.log(tr('runtime.wavefn.fukui.complete', { descriptor: which },
        'Fukui/CDFT({descriptor})完成', 'Fukui/CDFT ({descriptor}) complete'), 'okc');
    } else {
      if (out) out.textContent = res.error || tr('runtime.wavefn.fukui.engine_pending', {},
        '引擎待扩展;可复制 stdin 脚本手动运行',
        'Engine support is pending; copy the stdin script to run manually');
      if (res.script) VCS.log(tr('runtime.wavefn.fukui.script', {
        script: res.script.slice(0, 80),
      }, 'Fukui/CDFT 脚本:\n{script}…', 'Fukui/CDFT script:\n{script}…'), 'warnc');
    }
  }

  // ── NCI/IRI 散点 ──
  async function scatter() {
    const f1 = ($('wf-sc-f1') && $('wf-sc-f1').value.trim()) || '';
    const f2 = ($('wf-sc-f2') && $('wf-sc-f2').value.trim()) || '';
    if (!f1 || !f2) {
      VCS.log(tr('runtime.wavefn.scatter.files_required', {},
        '散点图:请指定 func1(sign(λ2)ρ)与 func2(RDG)cube',
        'Scatter plot: specify the func1 (sign(λ2)ρ) and func2 (RDG) cubes'), 'failc'); return;
    }
    const kind = ($('wf-sc-kind') && $('wf-sc-kind').value) || 'nci';
    VCS.log(tr('runtime.wavefn.scatter.generating', { kind: kind.toUpperCase() },
      '生成 {kind} 散点(RDG vs sign(λ2)ρ)…',
      'Generating {kind} scatter plot (RDG vs sign(λ2)ρ)…'));
    const r = await VCS.call('wavefn_scatter', f1, f2, kind, 3000, null);
    const out = $('wf-scatter-out');
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.wavefn.scatter.failed', {
        error: (r && r.error) || tr('runtime.wavefn.common.unknown', {}, '未知', 'Unknown'),
      }, '散点失败:{error}', 'Scatter generation failed: {error}'), 'failc'); return;
    }
    renderScatter(r.points || [], kind, out);
    VCS.log(tr('runtime.wavefn.scatter.complete', { kind: kind.toUpperCase(), count: r.n },
      '{kind} 散点:{count} 点', '{kind} scatter: {count} points'), 'okc');
  }
  function renderScatter(points, kind, out) {
    if (!out) return;
    out.innerHTML = '<div id="wf-scatter-chart" style="height:280px"></div>' +
      `<div class="sub">${VCS.esc(tr('runtime.wavefn.scatter.summary', {
        kind: kind.toUpperCase(), count: points.length,
      }, '{kind} 散点:横轴 sign(λ₂)ρ(a.u.),纵轴 RDG;共 {count} 点',
      '{kind} scatter: x-axis sign(λ₂)ρ (a.u.), y-axis RDG; {count} points'))}</div>`;
    if (typeof echarts === 'undefined') {
      out.querySelector('#wf-scatter-chart').textContent = tr(
        'runtime.wavefn.common.echarts_unavailable', {},
        '(echarts 未加载)', '(echarts is not loaded)');
      return;
    }
    const ch = echarts.init(out.querySelector('#wf-scatter-chart'));
    ch.setOption({
      grid: { left: 48, right: 16, top: 16, bottom: 40 },
      xAxis: { name: 'sign(λ2)ρ', type: 'value', min: -0.05, max: 0.05 },
      yAxis: { name: 'RDG', type: 'value', min: 0, max: 2 },
      series: [{ type: 'scatter', symbolSize: 3, data: points.map(p => [p.x, p.y]) }],
    });
  }
  async function espBar() {
    const d = ($('wf-out-dir') && $('wf-out-dir').value.trim()) || '';
    if (!d) {
      VCS.log(tr('runtime.wavefn.esp.output_required', {},
        'ESP 面积分布:请选 output 文件夹',
        'ESP surface distribution: select the output folder'), 'failc'); return;
    }
    // output 可视化:选文件夹 → 取其中 ESP 极值(esp_extrema)作面积分布柱状(经查询极值近似)
    VCS.log(tr('runtime.wavefn.esp.use_extrema', {},
      'output 可视化(ESP 面积分布)当前经「查询极值」呈现,请在上方选波函数文件后查询极值',
      'Output visualization for ESP surface distribution is currently shown through Query extrema; select a wavefunction file above, then query extrema'), 'warnc');
    VCS.toast(tr('runtime.wavefn.esp.folder_recorded', {},
      '已记录 output 文件夹;ESP 面积分布见极值查询',
      'Output folder recorded; use the extrema query for the ESP surface distribution'));
  }

  // ── 能级 / BCP 查询(可复制) ──
  async function queryHomoLumo() {
    const wf = ($('wf-file') && $('wf-file').value.trim()) || '';
    if (!wf) {
      VCS.log(tr('runtime.wavefn.homo_lumo.file_required', {},
        '能级查询:请先选波函数文件',
        'Energy-level query: select a wavefunction file first'), 'failc'); return;
    }
    const r = await VCS.call('wavefn_run', wf, ['homo_lumo_cube'], analysisParams(), null, null);
    const res = (r && r.results && r.results[0]) || {};
    const el = $('wf-homolumo-val');
    const txt = res.ok ? tr('runtime.wavefn.homo_lumo.exported', {
      outputs: (res.outputs || []).join(', '),
    }, '已导出 HOMO/LUMO 轨道 cube:{outputs}',
    'Exported HOMO/LUMO orbital cubes: {outputs}')
      : (res.error || tr('runtime.wavefn.common.multiwfn_required', {},
        '需 Multiwfn', 'Multiwfn is required'));
    if (el) el.textContent = txt;
    const cp = $('wf-homolumo-copy');
    if (cp) { cp.hidden = false; cp.onclick = () => copyText(txt); }
    VCS.log('HOMO-LUMO:' + txt, res.ok ? 'okc' : 'warnc');
  }
  // v3.3.0:切换 HOMO⇄LUMO(重导轨道 cube;对齐 starpivot「切换轨道」)
  function toggleHomoLumo() {
    const inp = $('wf-orbital');
    const cur = ((inp && inp.value.trim()) || 'HOMO').toUpperCase();
    const next = cur === 'LUMO' ? 'HOMO' : 'LUMO';
    if (inp) inp.value = next;
    VCS.log(tr('runtime.wavefn.homo_lumo.switched', { orbital: next },
      '轨道切换为 {orbital},重新导出 cube…',
      'Orbital switched to {orbital}; exporting the cube again…'));
    queryHomoLumo();
  }
  // v3.3.0:BCP 查询升级为结构化表(wavefn_bcp:ρ / V(r) / Espinosa 键能估算)
  async function queryBcp() {
    const wf = ($('wf-file') && $('wf-file').value.trim()) || '';
    if (!wf) {
      VCS.log(tr('runtime.wavefn.bcp.file_required', {},
        'BCP 查询:请先选波函数文件', 'BCP query: select a wavefunction file first'), 'failc'); return;
    }
    const el = $('wf-bcp-val');
    if (el) el.textContent = tr('runtime.wavefn.bcp.analyzing', {},
      'AIM 拓扑分析中…', 'Running AIM topology analysis…');
    const r = await VCS.call('wavefn_bcp', wf, null, null);
    const tbl = $('wf-bcp-table');
    if (!r || r.ok === false || r.error) {
      const txt = (r && r.error) || tr('runtime.wavefn.common.multiwfn_required', {},
        '需 Multiwfn', 'Multiwfn is required');
      if (el) el.textContent = txt;
      if (tbl) tbl.innerHTML = '';
      if (r && r.script) VCS.log(tr('runtime.wavefn.bcp.manual_script', {
        script: r.script.slice(0, 80),
      }, 'AIM stdin 脚本(可手跑):\n{script}…',
      'AIM stdin script (run manually):\n{script}…'), 'warnc');
      VCS.log('AIM BCP:' + txt, 'warnc');
      return;
    }
    const cps = r.cps || [];
    const bcps = cps.filter(c => c.type === '(3,-1)');
    const head = tr('runtime.wavefn.bcp.summary', {
      criticalPoints: cps.length, bondPoints: bcps.length, note: r.note || '',
    }, 'CP 共 {criticalPoints} 个(键 BCP {bondPoints} 个);{note}',
    '{criticalPoints} CPs ({bondPoints} bond BCPs); {note}');
    if (el) el.textContent = head;
    if (tbl) {
      tbl.innerHTML = `<table style="margin-top:6px"><thead><tr><th>#</th><th>${VCS.esc(tr(
        'runtime.wavefn.bcp.type', {}, '类型', 'Type',
      ))}</th>` +
        '<th class="num">ρ (a.u.)</th><th class="num">V(r) (a.u.)</th>' +
        `<th class="num">${VCS.esc(tr('runtime.wavefn.bcp.bond_energy', {},
          '键能估算 (kcal/mol)', 'Estimated bond energy (kcal/mol)',
        ))}</th><th class="mono">${VCS.esc(tr('runtime.wavefn.bcp.coordinates', {},
          '坐标 (Å)', 'Coordinates (Å)',
        ))}</th></tr></thead><tbody>` +
        cps.map(c => '<tr><td>' + c.index + '</td><td>' + VCS.esc(c.type) + '</td>' +
          '<td class="num">' + (c.rho != null ? (+c.rho).toFixed(4) : '—') + '</td>' +
          '<td class="num">' + (c.v != null ? (+c.v).toFixed(4) : '—') + '</td>' +
          '<td class="num">' + (c.bond_energy_kcal != null ? c.bond_energy_kcal : '—') + '</td>' +
          '<td class="mono">' + (c.xyz_angst ? c.xyz_angst.map(v => (+v).toFixed(2)).join(', ') : '—') +
          '</td></tr>').join('') + '</tbody></table>';
    }
    // CPprop 路径喂给可视化 aim 场景(选中场景后 cpprop 角色自动回填)
    if (r.cpprop_path && State.outputs.indexOf(r.cpprop_path) < 0) {
      State.outputs.push(r.cpprop_path);
      renderFromOutputs();
    }
    const cp = $('wf-bcp-copy');
    if (cp) {
      cp.hidden = false;
      const plain = cps.map(c => c.index + ' ' + c.type + ' rho=' + c.rho +
        ' V=' + c.v + ' E≈' + c.bond_energy_kcal + ' kcal/mol').join('\n');
      cp.onclick = () => copyText(plain + '\n' + (r.note || ''));
    }
    VCS.log('AIM BCP:' + head, 'okc');
  }
  async function copyText(t) {
    try { if (navigator.clipboard) await navigator.clipboard.writeText(t); } catch (e) { /* 忽略 */ }
    VCS.toast(tr('runtime.wavefn.common.copied', {}, '已复制', 'Copied'));
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
    // 渲染位置切换 + 新可视化工具(Fukui/散点/ESP/能级/BCP)
    document.querySelectorAll('input[name="wf-render-loc"]').forEach(
      r => r.addEventListener('change', onRenderLocChange));
    document.querySelectorAll('.wf-fukui').forEach(b => b.addEventListener('click', () => runFukui(b.dataset.f)));
    wire('wf-scatter-btn', scatter);
    wire('wf-out-btn', espBar);
    wire('wf-homolumo-btn', queryHomoLumo);
    wire('wf-hl-toggle', toggleHomoLumo);
    wire('wf-bcp-btn', queryBcp);
    wire('wf-elflol-link', () => {
      State.sel.add('elf_lol_section');
      showTab('analyze'); renderAnalysisChips();
      const plane = $('wf-plane-row'); if (plane) plane.hidden = false;
      VCS.toast(tr('runtime.wavefn.analysis.elf_lol_selected', {},
        '已选中 ELF/LOL 截面,设好切面后运行分析',
        'ELF/LOL section selected; configure the plane, then run the analysis'));
    });
    document.querySelectorAll('.wf-sc-browse').forEach(b => b.addEventListener('click', async () => {
      const r = await VCS.call('pick_file', 'cube');
      if (r && r.path) { const inp = $(b.dataset.t === 'f1' ? 'wf-sc-f1' : 'wf-sc-f2'); if (inp) inp.value = r.path; }
    }));
    // 产物带入可视化
    const fo = $('wf-fromoutputs');
    if (fo) fo.addEventListener('click', e => {
      const c = e.target.closest('[data-out]');
      if (!c) return;
      if ($('wf-vis-file')) $('wf-vis-file').value = c.dataset.out;
      const roles = sceneRoles();
      const empty = document.querySelector('#wf-scene-files .wf-role-in:not([data-filled])');
      if (roles.length && empty) { empty.value = c.dataset.out; empty.setAttribute('data-filled', '1'); }
      VCS.toast(tr('runtime.wavefn.outputs.transferred', {},
        '已带入可视化文件', 'Visualization file transferred'));
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
    document.addEventListener('vcs:language', () => {
      renderAnalysisChips();
      renderSceneChips();
      renderSceneFiles();
      renderFromOutputs();
      selectScene(State.scene);
    });
  }

  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'wavefunction') { init(); loadToolPaths(); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Wavefn = { reload: loadCatalog };
})();
