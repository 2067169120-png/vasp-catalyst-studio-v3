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
    'bands', 'eos', 'workfunction', 'dimer', 'freq', 'aimd',
    'conv_encut', 'conv_kmesh', 'conv_vacuum', 'conv_thickness']);
  const PARAMS = {
    eos: [{ k: 'scales', label: '缩放因子(逗号分隔,留空=默认7点)', ph: '0.96, 0.98, 1.0, 1.02, 1.04', list: 'float' }],
    bands: [{ k: 'lattice', label: '晶格类型', sel: ['', 'fcc', 'bcc', 'hcp', 'sc', 'tet', 'orc'] },
      { k: 'npoints', label: '每段 k 点数', ph: '40', num: true }],
    conv_encut: [{ k: 'values', label: 'ENCUT 值(逗号分隔)', ph: '400, 450, 500, 550', list: 'int' }],
    conv_kmesh: [{ k: 'meshes', label: 'k 网格(每行一个,如 3 3 1)', ph: '3 3 1\n5 5 1\n7 7 1', meshes: true }],
    conv_vacuum: [{ k: 'vacuums', label: '真空厚度 Å(逗号分隔)', ph: '10, 12, 15, 18', list: 'float' }],
    conv_thickness: [{ k: 'layers', label: '层数(逗号分隔)', ph: '3, 4, 5', list: 'int' }],
    aimd: [{ k: 'ensemble', label: '系综', sel: ['nvt', 'nve'] }, { k: 'temp_k', label: '温度 K', ph: '300', num: true },
      { k: 'steps', label: '步数', ph: '10000', num: true }, { k: 'potim_fs', label: '步长 fs', ph: '1.0', num: true }],
    dimer: [{ k: 'amplitude', label: '初始位移幅度 Å', ph: '0.01', num: true }],
    cellopt: [{ k: 'bump_encut', label: 'ENCUT×1.3(缓 Pulay 应力)', check: true }],
  };

  // ── ② 计算类型目录 ──
  async function loadCatalog() {
    const r = await VCS.call('task_catalog');
    if (!r || r.ok === false) { VCS.log('加载计算类型目录失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    State.cats = r.categories || [];
    State.tasks = r.tasks || [];
    State.cat = State.cats[0] || null;
    renderCats();
    renderGrid();
  }
  function renderCats() {
    const box = $('tc-cats');
    if (!box) return;
    box.innerHTML = State.cats.map(c =>
      `<span class="tc-cat${c === State.cat ? ' on' : ''}" data-cat="${VCS.esc(c)}">${VCS.esc(c)}</span>`).join('');
  }
  function renderGrid() {
    const box = $('tc-grid');
    if (!box) return;
    const items = State.tasks.filter(t => !State.cat || t.category === State.cat);
    box.innerHTML = items.map(t => {
      const badges = [];
      if (t.requires) badges.push(`<span class="tc-badge" title="前置条件">前置:${VCS.esc(t.requires)}</span>`);
      if (t.figure) badges.push(`<span class="tc-badge fig" title="关联图型">图:${VCS.esc(t.figure)}</span>`);
      const dv = DERIVABLE.has(t.key) ? '' : '<span class="tc-badge">专用流程</span>';
      return `<div class="tc-card${State.sel === t.key ? ' on' : ''}" data-key="${VCS.esc(t.key)}">` +
        `<b>${VCS.esc(t.name_zh)}</b><div class="tc-desc">${VCS.esc(t.description)}</div>` +
        `<div class="tc-badges">${badges.join('')}${dv}</div></div>`;
    }).join('') || '<span class="sub">此分类暂无任务</span>';
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
    if ($('tc-sel-badges')) $('tc-sel-badges').innerHTML =
      `<span class="tc-badge">产物:${VCS.esc(t.outputs || '—')}</span>` +
      (DERIVABLE.has(key) ? '' : '<span class="tc-badge">此类型经专用流程建作业(非一键派生)</span>');
    renderParams(key);
    const ubox = $('tc-u-box'); if (ubox) ubox.hidden = false;    // DFT+U 建议表(可选助手,始终可用)
    const uout = $('tc-u-out'); if (uout) uout.innerHTML = '';
    const uapply = $('tc-u-apply'); if (uapply) uapply.hidden = true;
    const dbtn = $('tc-derive-btn');
    if (dbtn) dbtn.disabled = !DERIVABLE.has(key);
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
    const src = (($('tc-src') && $('tc-src').value) || '').trim();
    if (!key) { VCS.log('请先选择计算类型', 'failc'); return; }
    if (!DERIVABLE.has(key)) { VCS.log('该类型经专用流程建作业,非一键派生', 'failc'); return; }
    if (!src) { VCS.log('请填源作业目录', 'failc'); return; }
    const btn = $('tc-derive-btn');
    if (btn) btn.disabled = true;
    VCS.log('派生作业(' + key + ')…');
    try {
      const r = await VCS.call('derive_task', key, src, collectParams(key));
      if (!r || r.ok === false || r.error) { VCS.log('派生失败:' + ((r && r.error) || '未知'), 'failc'); return; }
      const n = (r.job_dirs || []).length;
      VCS.log('已派生 ' + n + ' 个作业并入台账:' + (r.job_dirs || []).map(d => d.split(/[\\/]/).pop()).join('、'), 'okc');
      (r.warnings || []).forEach(w => VCS.log('提示:' + w, 'warnc'));
      VCS.toast('已派生 ' + n + ' 个作业');
    } finally { if (btn) btn.disabled = false; }
  }

  // ── ④ 任务解析 ──
  async function analyze() {
    const d = (($('ta-dir') && $('ta-dir').value) || '').trim();
    if (!d) { VCS.log('请选择作业目录', 'failc'); return; }
    const kind = ($('ta-kind') && $('ta-kind').value) || '';
    const btn = $('ta-run');
    if (btn) btn.disabled = true;
    VCS.log('任务解析…');
    try {
      const r = await VCS.call('analyze_task', d, kind || null);
      const out = $('ta-out');
      if (!r || r.ok === false || r.error) {
        VCS.log('解析失败:' + ((r && r.error) || '未知'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc((r && r.error) || '解析失败')}</div>`;
        return;
      }
      let h = `<div class="ta-summary"><b>${VCS.esc(r.kind)}</b> — ${VCS.esc(r.summary || '')}</div>`;
      if (r.figure) h += `<img class="ta-img" src="file://${VCS.esc(r.figure)}" alt="figure">` +
        `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.figure)}">打开目录</button></div>`;
      if (out) out.innerHTML = h;
      VCS.log('解析完成:' + (r.summary || r.kind), 'okc');
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
    if (!r || r.ok === false || r.error) { VCS.log('表面能计算失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    if (out) out.innerHTML = `<div class="ta-summary">表面能 <b>γ = ${r.gamma_jm2.toFixed(4)} J/m²</b>` +
      ` · 面积 ${r.area_a2.toFixed(2)} Å² · N=${r.n_slab} · E_bulk/atom=${r.e_bulk_per_atom.toFixed(4)} eV</div>`;
    VCS.log('γ = ' + r.gamma_jm2.toFixed(4) + ' J/m²', 'okc');
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
    wire('ta-se-run', surfEnergy);
    const out = $('ta-out');
    if (out) out.addEventListener('click', e => {
      const b = e.target.closest('[data-open]'); if (b) VCS.call('open_dir', b.dataset.open);
    });
    loadCatalog();
  }

  document.addEventListener('vcs:page', e => {
    if (!e.detail) return;
    if (e.detail.page === 'generate' || e.detail.page === 'project') init();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
  window.TaskCat = { reload: loadCatalog };
})();
