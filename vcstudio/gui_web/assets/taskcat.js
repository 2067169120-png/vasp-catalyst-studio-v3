// taskcat.js — 全 DFT 任务目录(②生成输入)+ 任务解析(④结果分析)。
// ② 计算类型目录:五分类卡片网格 → 参数表单 + DFT+U 建议 → derive_task 派生作业。
// ④ 任务解析:analyze_task 按 task_type 自动解析 + 出图;surface_energy_calc 表面能计算器。
// 只依赖 app.js 的 VCS.*;插值走 VCS.esc,零 emoji。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const State = { cats: [], tasks: [], cat: null, sel: null, uSugg: null };

  // 可一键派生的任务(其余走各自专用流程)+ 每任务参数表单 schema
  const DERIVABLE = new Set(['cellopt', 'static', 'dos_pdos', 'bader', 'chgdiff', 'elf',
    'bands', 'eos', 'workfunction', 'dimer', 'freq', 'aimd', 'vaspsol',
    'conv_encut', 'conv_kmesh', 'conv_vacuum', 'conv_thickness']);
  // 有专用面板/计算器的任务:点「派生」不是死按钮,而是跳转/展开对应面板(P0-1 / P0-2)。
  const ROUTED = {
    relax: { label: '前往四件套输入', run: gotoRelax },
    adsorption_project: { label: '前往吸附能全流程', run: gotoAdsorption },
    spin_scan: { label: '前往多自旋扫描', run: gotoSpin },
    neb: { label: '前往 NEB 面板', run: expandNeb },
    formation_binding: { label: '前往形成能/结合能计算器', run: gotoCalc },
    surface_energy: { label: '前往表面能计算器', run: gotoSurfaceEnergy },
  };
  const PARAMS = {
    eos: [{ k: 'scales', label: '缩放因子(逗号分隔,留空=默认7点)', ph: '0.96, 0.98, 1.0, 1.02, 1.04', list: 'float' }],
    bands: [{ k: 'lattice', label: '晶格类型', sel: ['', 'fcc', 'bcc', 'hcp', 'sc', 'tet', 'orc'] },
      { k: 'npoints', label: '每段 k 点数', ph: '40', num: true }],
    chgdiff: [{ k: 'adsorbate_indices', label: '吸附质原子序号(1 起,逗号分隔;据此拆 AB/A/B)', ph: '27, 28', list: 'int' }],
    conv_encut: [{ k: 'values', label: 'ENCUT 值(逗号分隔)', ph: '400, 450, 500, 550', list: 'int' }],
    conv_kmesh: [{ k: 'meshes', label: 'k 网格(每行一个,如 3 3 1)', ph: '3 3 1\n5 5 1\n7 7 1', meshes: true }],
    conv_vacuum: [{ k: 'vacuums', label: '真空厚度 Å(逗号分隔)', ph: '10, 12, 15, 18', list: 'float' }],
    conv_thickness: [{ k: 'layers', label: '层数(逗号分隔)', ph: '3, 4, 5', list: 'int' }],
    aimd: [{ k: 'ensemble', label: '系综', sel: ['nvt', 'nve'] }, { k: 'temp_k', label: '温度 K', ph: '300', num: true },
      { k: 'steps', label: '步数', ph: '10000', num: true }, { k: 'potim_fs', label: '步长 fs', ph: '1.0', num: true }],
    dimer: [{ k: 'amplitude', label: '初始位移幅度 Å', ph: '0.01', num: true }],
    cellopt: [{ k: 'bump_encut', label: 'ENCUT×1.3(缓 Pulay 应力)', check: true }],
    vaspsol: [{ k: 'eb_k', label: '溶剂相对介电常数 EB_K', ph: '78.4', num: true }],
  };
  // 任务性质徽标底色(P2:作业生成 / 结果计算器 / INCAR 顾问)
  const BADGE_CLS = { '作业生成': 'gen', '结果计算器': 'calc', 'INCAR 顾问': 'advisor' };
  const ANALYSIS_LABEL = {
    integrated: '自动解析 + 报告', evidence_only: '产物核对 + 报告',
    dedicated: '专用流程 + 报告', unsupported: '暂不支持分析',
  };

  // ── ② 计算类型目录 ──
  async function loadCatalog() {
    const sceneKey = (VCS.scenario && VCS.scenario.key) || null;
    const active = VCS.activeCalculation || null;
    const r = await VCS.call('task_catalog', sceneKey, active, VCS.activeEngine || 'vasp');
    if (!r || r.ok === false) { VCS.log('加载计算类型目录失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    State.cats = r.categories || [];
    State.tasks = r.tasks || [];
    State.cat = State.cats[0] || null;
    State.sel = null;
    const form = $('tc-form'); if (form) form.hidden = true;
    renderCats();
    renderGrid();
    if (State.tasks.length === 1) selectTask(State.tasks[0].key);
    syncAnalysisKind();
  }
  // 分类 optgroup 下拉替代"分类 chips + 卡片网格墙"(VASP 23 类计算目录)。
  // 保留 tc-cats(承载下拉)与 tc-grid(隐藏,id 保持)容器。
  function buildOptions(sel) {
    const cats = State.cats.length ? State.cats
      : Array.from(new Set(State.tasks.map(t => t.category)));
    let html = '<option value="">选择计算类型…</option>';
    cats.forEach(c => {
      const items = State.tasks.filter(t => t.category === c);
      if (!items.length) return;
      html += `<optgroup label="${VCS.esc(c)}">` + items.map(t =>
        `<option value="${VCS.esc(t.key)}"${State.sel === t.key ? ' selected' : ''}>` +
        `${VCS.esc(t.name_zh)}${DERIVABLE.has(t.key) ? '' : '(专用流程)'}</option>`).join('') + '</optgroup>';
    });
    sel.innerHTML = html;
  }
  function renderCats() {
    const box = $('tc-cats');
    if (!box) return;
    let sel = document.getElementById('tc-select');
    if (!sel) {
      sel = document.createElement('select');
      sel.id = 'tc-select';
      sel.className = 'ipt';
      box.innerHTML = '';
      box.appendChild(sel);
      sel.addEventListener('change', () => { if (sel.value) selectTask(sel.value); });
    }
    buildOptions(sel);
  }
  function renderGrid() {
    const box = $('tc-grid');
    if (box) { box.hidden = true; box.innerHTML = ''; }   // 网格墙隐藏,改由上方 optgroup 下拉选择
    const sel = document.getElementById('tc-select');
    if (sel) buildOptions(sel);
  }
  function selectTask(key) {
    State.sel = key;
    State.uSugg = null;
    renderGrid();
    const t = State.tasks.find(x => x.key === key);
    const form = $('tc-form');
    if (!t || !form) return;
    form.hidden = false;
    if ($('tc-sel-name')) $('tc-sel-name').textContent = t.name_zh;
    if ($('tc-sel-desc')) $('tc-sel-desc').textContent = t.description;
    const kind = t.kind_badge || '作业生成';
    const routed = !!ROUTED[key];
    if ($('tc-sel-badges')) $('tc-sel-badges').innerHTML =
      `<span class="tc-badge tc-badge-${BADGE_CLS[kind] || 'gen'}">${VCS.esc(kind)}</span>` +
      `<span class="tc-badge">${VCS.esc(ANALYSIS_LABEL[t.analysis_status] || '分析能力待确认')}</span>` +
      `<span class="tc-badge">产物:${VCS.esc(t.outputs || '—')}</span>` +
      (DERIVABLE.has(key) || routed ? '' : '<span class="tc-badge">此类型经专用流程建作业(非一键派生)</span>');
    renderParams(key);
    const ubox = $('tc-u-box'); if (ubox) ubox.hidden = false;    // DFT+U 建议表(可选助手,始终可用)
    const uout = $('tc-u-out'); if (uout) uout.innerHTML = '';
    const uapply = $('tc-u-apply'); if (uapply) uapply.hidden = true;
    const dbtn = $('tc-derive-btn');
    if (dbtn) {
      // 有专用面板的任务(NEB / 形成能结合能):按钮改为「前往…」跳转,不再是禁用死按钮。
      dbtn.disabled = !DERIVABLE.has(key) && !routed;
      dbtn.textContent = routed ? ROUTED[key].label : '派生作业';
    }
  }
  // 跳转/展开专用面板(替代 neb / formation_binding 的禁用死按钮)
  function expandNeb() {
    const card = document.getElementById('neb-card');
    if (card) {
      card.setAttribute('data-open', '1');
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    VCS.toast('已展开 NEB 面板:填始态/末态目录后「生成 NEB」');
  }
  function focusAfterNavigate(page, selector, message) {
    VCS.navigate(page, { source: 'task-catalog', focusSelector: selector }).then(out => {
      if (out.ok && message) VCS.toast(message);
    });
  }
  function gotoRelax() {
    focusAfterNavigate('generate', '#gen-poscar', '选择 POSCAR、INCAR 和输出目录后生成四件套');
  }
  function gotoAdsorption() {
    focusAfterNavigate('project', '#ads-journey', '已进入吸附能全流程');
  }
  function gotoSpin() {
    focusAfterNavigate('structure', '#spin-card', '已定位多自旋态扫描');
  }
  function gotoCalc() {
    const nav = document.querySelector('nav a[data-page="project"]');
    if (nav) nav.click();
    setTimeout(() => {
      const c = document.getElementById('fb-card');
      if (c) c.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 120);
    VCS.toast('已跳转到结果分析页的形成能/结合能计算器');
  }
  function gotoSurfaceEnergy() {
    const ana = $('analysis-type');
    if (ana) { ana.value = 'taskana'; ana.dispatchEvent(new Event('change', { bubbles: true })); }
    focusAfterNavigate('project', '#ta-se-slab', '选择 slab 与同口径 bulk 作业后计算表面能');
  }
  function renderParams(key) {
    const box = $('tc-params');
    if (!box) return;
    const schema = PARAMS[key] || [];
    box.innerHTML = schema.map(f => {
      if (f.check) return `<label class="gen-lbl" style="display:flex;gap:6px;align-items:center;margin-bottom:8px">` +
        `<input type="checkbox" id="tcp-${f.k}" checked> ${VCS.esc(f.label)}</label>`;
      let field;
      if (f.sel) field = `<select id="tcp-${f.k}" class="ipt">` +
        f.sel.map(o => `<option value="${VCS.esc(o)}">${VCS.esc(o || '自动')}</option>`).join('') + '</select>';
      else if (f.meshes) field = `<textarea id="tcp-${f.k}" class="ipt" rows="3" placeholder="${VCS.esc(f.ph)}"></textarea>`;
      else field = `<input id="tcp-${f.k}" class="ipt" placeholder="${VCS.esc(f.ph || '')}">`;
      return `<div class="gen-fld"><span class="gen-lbl">${VCS.esc(f.label)}</span>${field}</div>`;
    }).join('');
  }
  function collectParams(key) {
    const p = {};
    (PARAMS[key] || []).forEach(f => {
      const el = $('tcp-' + f.k);
      if (!el) return;
      if (f.check) { p[f.k] = el.checked; return; }
      const v = (el.value || '').trim();
      if (!v) return;
      if (f.num) p[f.k] = parseFloat(v);
      else if (f.list) p[f.k] = v.split(',').map(s => s.trim()).filter(Boolean)
        .map(s => f.list === 'int' ? parseInt(s, 10) : parseFloat(s));
      else if (f.meshes) p[f.k] = v.split('\n').map(l => l.trim().split(/\s+/).map(Number))
        .filter(a => a.length === 3);
      else p[f.k] = v;
    });
    return p;
  }
  async function queryU() {
    const els = (($('tc-u-els') && $('tc-u-els').value) || '').split(',').map(s => s.trim()).filter(Boolean);
    if (!els.length) { VCS.log('请填元素符号', 'failc'); return; }
    const r = await VCS.call('u_suggest', els);
    const out = $('tc-u-out');
    if (!r || r.ok === false) { if (out) out.innerHTML = `<span class="sub">查询失败:${VCS.esc((r && r.error) || '')}</span>`; return; }
    State.uSugg = r;
    let h = (r.suggestions || []).map(s =>
      `<div class="urow"><b>${VCS.esc(s.element)}</b> U=${s.u} eV(${VCS.esc(s.orbital)})· ${VCS.esc(s.source)}` +
      `<span class="sub" title="${VCS.esc(s.note)}"> ⓘ</span></div>`).join('');
    if ((r.missing || []).length) h += `<div class="sub" style="color:var(--warn)">库内无经验 U(不编造):${(r.missing).join('、')}</div>`;
    if (out) out.innerHTML = h || '<span class="sub">无匹配元素</span>';
    const apply = $('tc-u-apply');
    if (apply) apply.hidden = !(r.suggestions || []).length;
  }
  function applyU() {
    if (!State.uSugg || !State.uSugg.incar_keys) return;
    const keys = State.uSugg.incar_keys;
    const lines = Object.keys(keys).map(k => `${k} = ${keys[k] === true ? '.TRUE.' : keys[k]}`).join('\n');
    const kw = $('gen-extra-kw');
    if (kw) { kw.value = (kw.value ? kw.value + '\n' : '') + lines; VCS.toast('已把 LDAU 键追加到②自定义关键词'); }
    VCS.log('DFT+U:已把 LDAU 系列键写入生成页自定义关键词', 'okc');
  }
  async function derive() {
    const key = State.sel;
    if (!key) { VCS.log('请先选择计算类型', 'failc'); return; }
    if (ROUTED[key]) { ROUTED[key].run(); return; }       // 专用面板:跳转/展开,不走一键派生
    const src = (($('tc-src') && $('tc-src').value) || '').trim();
    if (!DERIVABLE.has(key)) { VCS.log('该类型经专用流程建作业,非一键派生', 'failc'); return; }
    if (!src) { VCS.log('请填源作业目录', 'failc'); return; }
    const btn = $('tc-derive-btn');
    if (btn) btn.disabled = true;
    VCS.log('派生作业(' + key + ')…');
    try {
      const r = await VCS.call('derive_task', key, src, collectParams(key));
      if (!r || r.ok === false || r.error) { VCS.log('派生失败:' + ((r && r.error) || '未知'), 'failc'); return; }
      const n = (r.job_dirs || []).length;
      // 诚实化:0 作业 + note(如层厚收敛需从建 slab 流程发起)→ warn 级说明,绝不报成功 toast。
      if (n === 0 && r.note) {
        VCS.log('未生成作业:' + r.note, 'warnc');
        (r.warnings || []).forEach(w => { if (w !== r.note) VCS.log('提示:' + w, 'warnc'); });
        VCS.toast('未生成作业(见日志说明)');
        return;
      }
      VCS.log('已派生 ' + n + ' 个作业并入台账:' + (r.job_dirs || []).map(d => d.split(/[\\/]/).pop()).join('、'), 'okc');
      (r.warnings || []).forEach(w => VCS.log('提示:' + w, 'warnc'));
      VCS.toast('已派生 ' + n + ' 个作业');
      if (n && VCS.nextStep) VCS.nextStep({
        title: '派生作业已生成', message: `已创建 ${n} 个作业并加入任务列表。`,
        detail: '下一步：选择服务器、核数与墙时，然后上传并提交。',
        primaryLabel: '前往任务页提交', page: 'jobs', focusJobDir: (r.job_dirs || [])[0] || '',
      });
    } finally { if (btn) btn.disabled = false; }
  }

  // ── NEB 面板(P0-1):始/末态目录 + image 数 → derive_neb ──
  async function generateNeb() {
    const s = (($('neb-start') && $('neb-start').value) || '').trim();
    const e = (($('neb-end') && $('neb-end').value) || '').trim();
    const nimg = parseInt(($('neb-nimages') && $('neb-nimages').value) || '5', 10) || 5;
    if (!s) { VCS.log('NEB:请选始态作业目录', 'failc'); return; }
    if (!e) { VCS.log('NEB:请选末态作业目录', 'failc'); return; }
    const btn = $('neb-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log('生成 NEB 目录树(始态+末态,' + nimg + ' 个中间 image)…');
    try {
      const r = await VCS.call('derive_neb', s, e, nimg);
      const out = $('neb-out');
      if (!r || r.ok === false || r.error) {
        VCS.log('NEB 生成失败:' + ((r && r.error) || '未知'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc((r && r.error) || 'NEB 生成失败')}</div>`;
        return;
      }
      (r.changes || []).forEach(c => VCS.log(c, 'okc'));
      (r.warnings || []).forEach(w => VCS.log('提示:' + w, 'warnc'));
      VCS.log('NEB 目录树已生成并入台账:' + r.job_dir, 'okc');
      if (out) out.innerHTML = `<div class="ta-summary">已生成 NEB 目录:<b>${VCS.esc(r.job_dir)}</b>` +
        ` · 00 初态 + ${r.n_images} 中间 image + 末态` +
        `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.job_dir)}">打开目录</button></div></div>`;
      VCS.toast('已生成 NEB 目录树');
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      if (VCS.nextStep) VCS.nextStep({
        title: 'NEB 作业已生成', message: 'NEB 目录树已加入任务列表。',
        detail: '下一步：选择服务器并上传提交；任务页会按 NEB 目录结构处理。',
        primaryLabel: '前往任务页提交', page: 'jobs', focusJobDir: r.job_dir,
      });
    } finally { if (btn) btn.disabled = false; }
  }

  // ── 形成能/结合能计算器(P0-2)──
  function parseKV(s) {
    // 'Fe=-3.2, N=-8.3' → {Fe:-3.2, N:-8.3};非法项跳过
    const kv = {};
    (s || '').split(',').forEach(pair => {
      const m = pair.split('=');
      if (m.length === 2) {
        const el = m[0].trim(); const v = parseFloat(m[1].trim());
        if (el && !isNaN(v)) kv[el] = v;
      }
    });
    return kv;
  }
  async function formationBinding() {
    const sac = (($('fb-sac') && $('fb-sac').value) || '').trim();
    const sub = (($('fb-sub') && $('fb-sub').value) || '').trim();
    const atom = parseKV(($('fb-atom') && $('fb-atom').value) || '');
    const mu = parseKV(($('fb-mu') && $('fb-mu').value) || '');
    if (!sac) { VCS.log('形成能/结合能:请选 SAC 作业目录', 'failc'); return; }
    VCS.log('形成能/结合能计算…');
    const r = await VCS.call('formation_binding_calc', sac, sub || null,
      Object.keys(atom).length ? atom : null, Object.keys(mu).length ? mu : null);
    const out = $('fb-out');
    if (!r || r.error) {
      (r && r.warnings || []).forEach(warning => VCS.log('形成能/结合能:' + warning, 'warnc'));
      VCS.log('计算失败:' + ((r && r.error) || '未知'), 'failc');
      const warningHtml = (r && r.warnings || []).map(warning =>
        `<div class="sub" style="color:var(--warn);margin-top:4px">⚠ ${VCS.esc(warning)}</div>`).join('');
      if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc((r && r.error) || '计算失败')}${warningHtml}</div>`;
      return; }
    let h = '<div class="ta-summary">';
    if (r.e_sac != null) h += `E(SAC)=${(+r.e_sac).toFixed(4)} eV`;
    if (r.e_substrate != null) h += ` · E(基底)=${(+r.e_substrate).toFixed(4)} eV`;
    if (r.metal) h += ` · 金属=<b>${VCS.esc(r.metal)}</b>`;
    if (r.binding_energy != null) h += `<br>结合能 <b>Eb = ${(+r.binding_energy).toFixed(4)} eV</b>`;
    if (r.sigma != null) h += ` · σ = ${(+r.sigma).toFixed(3)}(${r.stable ? '判稳' : '判不稳'})`;
    if (r.formation_energy != null) h += `<br>形成能 <b>Ef = ${(+r.formation_energy).toFixed(4)} eV</b>`;
    if (r.stability_note) h += `<div class="sub" style="margin-top:4px">${VCS.esc(r.stability_note)}</div>`;
    (r.hints || []).forEach(hint => { h += `<div class="sub" style="color:var(--warn);margin-top:4px">${VCS.esc(hint)}</div>`; });
    (r.warnings || []).forEach(warning => {
      h += `<div class="sub" style="color:var(--warn);margin-top:4px">⚠ ${VCS.esc(warning)}</div>`;
      VCS.log('形成能/结合能:' + warning, 'warnc');
    });
    if (r.report_file) h += `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.report_file)}">打开可追溯报告</button></div>`;
    h += '</div>';
    if (out) out.innerHTML = h;
    if (r.binding_energy != null || r.formation_energy != null)
      VCS.log('形成能/结合能计算完成', 'okc');
    else VCS.log('缺输入,未算出 Eb/Ef(见提示)', 'warnc');
  }

  // ── 差分电荷合成(P0-4)──
  async function chgdiffSynth() {
    const ab = (($('cd-ab') && $('cd-ab').value) || '').trim();
    const a = (($('cd-a') && $('cd-a').value) || '').trim();
    const b = (($('cd-b') && $('cd-b').value) || '').trim();
    if (!ab || !a || !b) { VCS.log('差分电荷:请选 AB / A / B 三个作业目录', 'failc'); return; }
    const btn = $('cd-run');
    if (btn) btn.disabled = true;
    VCS.log('差分电荷合成 Δρ=ρ(AB)−ρ(A)−ρ(B)…');
    try {
      const r = await VCS.call('compute_chgdiff', ab, a, b, null);
      const out = $('cd-out');
      if (!r || r.ok === false || r.error) {
        VCS.log('差分电荷合成失败:' + ((r && r.error) || '未知'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc((r && r.error) || '合成失败')}</div>`;
        return;
      }
      let h = `<div class="ta-summary">已合成 <b>${VCS.esc(r.out)}</b>(VESTA 可读)` +
        ` · Δ(ρ×V) 极大 ${(+r.max).toExponential(3)} / 极小 ${(+r.min).toExponential(3)} · 网格 ${r.n_grid}` +
        `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.out)}">打开目录</button>` +
        (r.report_file ? `<button class="btn" data-open="${VCS.esc(r.report_file)}">打开可追溯报告</button>` : '') +
        `</div>` + (r.warnings || []).map(w => `<div class="sub" style="color:var(--warn)">⚠ ${VCS.esc(w)}</div>`).join('') + `</div>`;
      if (out) {
        out.innerHTML = h + '<div id="cd-chart" style="height:220px;margin-top:8px"></div>';
        renderChargeProfile(r.profile || {});
      }
      VCS.log('差分电荷合成完成:' + r.out, 'okc');
      (r.warnings || []).forEach(w => VCS.log('差分电荷:' + w, 'warnc'));
      VCS.toast('已合成差分电荷 Δρ');
    } finally { if (btn) btn.disabled = false; }
  }
  function renderChargeProfile(profile) {
    const el = document.getElementById('cd-chart');
    if (!el) return;
    const z = profile.z || []; const rho = profile.rho || [];
    if (!z.length || typeof echarts === 'undefined') { el.textContent = z.length ? '(echarts 未加载)' : '(无面平均数据)'; return; }
    const ch = echarts.init(el);
    ch.setOption({
      grid: { left: 56, right: 16, top: 16, bottom: 40 },
      xAxis: { name: 'z (Å)', type: 'value' },
      yAxis: { name: 'Δρ̄ (e/Å³)', type: 'value' },
      series: [{ type: 'line', showSymbol: false, data: z.map((v, i) => [v, rho[i]]) }],
    });
  }

  // ── ④ 任务解析 ──
  function selectedAnalysisKind() {
    return (($('ta-kind') && $('ta-kind').value) || '').trim();
  }

  function syncAnalysisKind() {
    const sel = $('ta-kind');
    if (!sel) return;
    const old = sel.querySelector('[data-active-calculation]');
    if (old) old.remove();
    const active = VCS.activeCalculation || '';
    if (!active) return;
    const task = State.tasks.find(row => row.key === active);
    const option = document.createElement('option');
    option.value = active;
    option.dataset.activeCalculation = '1';
    option.textContent = '按设置：' + ((task && task.name_zh) || active);
    sel.insertBefore(option, sel.children[1] || null);
    sel.value = active;
  }

  async function analyze() {
    const d = (($('ta-dir') && $('ta-dir').value) || '').trim();
    if (!d) { VCS.log('请选择作业目录', 'failc'); return; }
    const kind = selectedAnalysisKind();
    const btn = $('ta-run');
    if (btn) btn.disabled = true;
    VCS.log('任务解析…');
    try {
      const r = await VCS.call('analyze_task', d, kind || null);
      const out = $('ta-out');
      if (!r || r.ok === false || r.error) {
        VCS.log('解析失败:' + ((r && r.error) || '未知'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc((r && r.error) || '解析失败')}` +
          `${r && r.next_action ? `<div class="sub" style="margin-top:6px">下一步：${VCS.esc(r.next_action)}</div>` : ''}</div>`;
        return;
      }
      let h = `<div class="ta-summary"><b>${VCS.esc(r.kind)}</b> — ${VCS.esc(r.summary || '')}` +
        `${r.next_action ? `<div class="sub" style="margin-top:6px">建议：${VCS.esc(r.next_action)}</div>` : ''}</div>`;
      if (r.figure) h += `<img class="ta-img" src="file://${VCS.esc(r.figure)}" alt="figure">` +
        `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.figure)}">打开目录</button></div>`;
      if (out) out.innerHTML = h;
      VCS.log('解析完成:' + (r.summary || r.kind), 'okc');
    } finally { if (btn) btn.disabled = false; }
  }

  async function makeTaskReport() {
    const d = (($('ta-dir') && $('ta-dir').value) || '').trim();
    if (!d) { VCS.log('生成报告前请先选择作业目录', 'failc'); return; }
    const sep = d.includes('\\') ? '\\' : '/';
    const path = d.replace(/[\\/]$/, '') + sep + 'vcstudio-task-report.html';
    const btn = $('ta-report'); if (btn) btn.disabled = true;
    try {
      const r = await VCS.call('task_report', d, path, selectedAnalysisKind() || null);
      if (!r || r.ok === false || r.error) {
        VCS.log('报告生成失败:' + ((r && r.error) || '未知'), 'failc'); return;
      }
      VCS.log('可追溯报告已生成:' + r.file, r.analysis_ok ? 'okc' : 'warnc');
      VCS.nextStep({ title: '任务报告已生成',
        message: r.analysis_ok ? '解析结果和文件指纹已写入报告。' : '报告已记录缺失项和下一步，没有伪造分析结论。',
        detail: r.file, primaryLabel: '打开报告所在目录',
        onPrimary: () => VCS.call('open_dir', r.file) });
    } finally { if (btn) btn.disabled = false; }
  }
  async function surfEnergy() {
    const slab = (($('ta-se-slab') && $('ta-se-slab').value) || '').trim();
    const bulk = (($('ta-se-bulk') && $('ta-se-bulk').value) || '').trim();
    const eb = (($('ta-se-ebulk') && $('ta-se-ebulk').value) || '').trim();
    if (!slab) { VCS.log('请选 slab 作业', 'failc'); return; }
    VCS.log('表面能计算…');
    const r = await VCS.call('surface_energy_calc', slab, bulk, eb ? parseFloat(eb) : null, null);
    const out = $('ta-out');
    if (!r || r.ok === false || r.error) {
      (r && r.warnings || []).forEach(warning => VCS.log('表面能:' + warning, 'warnc'));
      VCS.log('表面能计算失败:' + ((r && r.error) || '未知'), 'failc'); return;
    }
    const warnings = (r.warnings || []).map(warning =>
      `<div class="sub" style="color:var(--warn);margin-top:4px">⚠ ${VCS.esc(warning)}</div>`).join('');
    const report = r.report_file
      ? `<div class="actions"><button class="btn" data-open="${VCS.esc(r.report_file)}">打开可追溯报告</button></div>` : '';
    if (out) out.innerHTML = `<div class="ta-summary">表面能 <b>γ = ${r.gamma_jm2.toFixed(4)} J/m²</b>` +
      ` · 面积 ${r.area_a2.toFixed(2)} Å² · N=${r.n_slab} · E_bulk/atom=${r.e_bulk_per_atom.toFixed(4)} eV${warnings}${report}</div>`;
    (r.warnings || []).forEach(warning => VCS.log('表面能:' + warning, 'warnc'));
    VCS.log('γ = ' + r.gamma_jm2.toFixed(4) + ' J/m²', 'okc');
  }

  async function vaspsolPair() {
    const vacuum = (($('vs-vacuum') && $('vs-vacuum').value) || '').trim();
    const solvent = (($('vs-solvent') && $('vs-solvent').value) || '').trim();
    if (!vacuum || !solvent) {
      VCS.log('VASPsol 配对：请选择真空与溶剂两个作业目录', 'failc'); return;
    }
    const btn = $('vs-run'); if (btn) btn.disabled = true;
    try {
      const r = await VCS.call('vaspsol_pair_calc', vacuum, solvent, null);
      const out = $('vs-out');
      if (!r || r.ok === false || r.error) {
        VCS.log('VASPsol 配对计算失败:' + ((r && r.error) || '未知'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc((r && r.error) || '计算失败')}</div>`;
        return;
      }
      if (out) out.innerHTML = `<div class="ta-summary">溶剂化能 <b>ΔE<sub>solv</sub> = ${(+r.solvation_energy_eV).toFixed(8)} eV</b>` +
        ` · E<sub>vac</sub>=${(+r.e_vacuum_eV).toFixed(8)} eV · E<sub>sol</sub>=${(+r.e_solvent_eV).toFixed(8)} eV` +
        `<div class="sub">已验证配对、方法一致性与 VASPsol OUTCAR 证据；报告：${VCS.esc(r.report_file)}</div>` +
        `<div class="actions"><button class="btn" data-open="${VCS.esc(r.report_file)}">打开报告所在目录</button></div></div>`;
      VCS.log('VASPsol 配对报告已生成:' + r.report_file, 'okc');
    } finally { if (btn) btn.disabled = false; }
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  async function browseInto(id, kind) {
    const r = await VCS.call(kind === 'dir' ? 'pick_dir' : 'pick_file', kind === 'dir' ? undefined : kind);
    if (r && r.path && $(id)) $(id).value = r.path;
  }
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    const cats = $('tc-cats');
    if (cats) cats.addEventListener('click', e => {
      const c = e.target.closest('.tc-cat'); if (!c) return;
      State.cat = c.dataset.cat; renderCats(); renderGrid();
    });
    const grid = $('tc-grid');
    if (grid) grid.addEventListener('click', e => {
      const c = e.target.closest('.tc-card'); if (c) selectTask(c.dataset.key);
    });
    wire('tc-src-btn', () => browseInto('tc-src', 'dir'));
    wire('tc-u-btn', queryU);
    wire('tc-u-apply', applyU);
    wire('tc-derive-btn', derive);
    // ④
    wire('ta-dir-btn', () => browseInto('ta-dir', 'dir'));
    wire('ta-se-slab-btn', () => browseInto('ta-se-slab', 'dir'));
    wire('ta-se-bulk-btn', () => browseInto('ta-se-bulk', 'dir'));
    wire('ta-run', analyze);
    wire('ta-report', makeTaskReport);
    wire('ta-spin-jobs', () => VCS.navigate('jobs', { source: 'spin-analysis' }));
    wire('ta-se-run', surfEnergy);
    wire('vs-vacuum-btn', () => browseInto('vs-vacuum', 'dir'));
    wire('vs-solvent-btn', () => browseInto('vs-solvent', 'dir'));
    wire('vs-run', vaspsolPair);
    // NEB 面板(②生成页):始/末态浏览 + 生成
    wire('neb-start-btn', () => browseInto('neb-start', 'dir'));
    wire('neb-end-btn', () => browseInto('neb-end', 'dir'));
    wire('neb-gen-btn', generateNeb);
    // 形成能/结合能计算器 + 差分电荷合成(④结果分析页)
    wire('fb-sac-btn', () => browseInto('fb-sac', 'dir'));
    wire('fb-sub-btn', () => browseInto('fb-sub', 'dir'));
    wire('fb-run', formationBinding);
    wire('cd-ab-btn', () => browseInto('cd-ab', 'dir'));
    wire('cd-a-btn', () => browseInto('cd-a', 'dir'));
    wire('cd-b-btn', () => browseInto('cd-b', 'dir'));
    wire('cd-run', chgdiffSynth);
    // 结果卡片「打开目录」委托(ta-out / neb-out / fb-out / cd-out 共用 data-open)
    ['ta-out', 'neb-out', 'fb-out', 'cd-out', 'vs-out'].forEach(oid => {
      const box = $(oid);
      if (box) box.addEventListener('click', e => {
        const b = e.target.closest('[data-open]'); if (b) VCS.call('open_dir', b.dataset.open);
      });
    });
    loadCatalog();
  }

  document.addEventListener('vcs:page', e => {
    if (!e.detail) return;
    if (e.detail.page === 'generate' || e.detail.page === 'project') init();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
  document.addEventListener('vcs:scenario', loadCatalog);
  document.addEventListener('vcs:engine', loadCatalog);
  document.addEventListener('vcs:calculation', loadCatalog);
  document.addEventListener('vcs:calculation', syncAnalysisKind);
  window.TaskCat = { reload: loadCatalog };
})();
