// taskcat.js — 全 DFT 任务目录(②生成输入)+ 任务解析(④结果分析)。
// ② 计算类型目录:五分类卡片网格 → 参数表单 + DFT+U 建议 → derive_task 派生作业。
// ④ 任务解析:analyze_task 按 task_type 自动解析 + 出图;surface_energy_calc 表面能计算器。
// 只依赖 app.js 的 VCS.*;插值走 VCS.esc,零 emoji。
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
  const uiEnglish = () => !!(VCS.i18n && VCS.i18n.lang === 'en');
  const taskText = (task, zhField, enField) => uiEnglish()
    ? (task[enField] || task[zhField] || '') : (task[zhField] || task[enField] || '');
  const State = { cats: [], tasks: [], cat: null, sel: null, uSugg: null };

  // 可一键派生的任务(其余走各自专用流程)+ 每任务参数表单 schema
  const DERIVABLE = new Set(['cellopt', 'static', 'dos_pdos', 'bader', 'chgdiff', 'elf',
    'bands', 'eos', 'workfunction', 'dimer', 'freq', 'aimd', 'vaspsol',
    'conv_encut', 'conv_kmesh', 'conv_vacuum', 'conv_thickness']);
  // 有专用面板/计算器的任务:点「派生」不是死按钮,而是跳转/展开对应面板(P0-1 / P0-2)。
  const ROUTED = {
    relax: { label: () => tr('runtime.taskcat.route.four_files', {},
      '前往四件套输入', 'Open four-file input'), run: gotoRelax },
    adsorption_project: { label: () => tr('runtime.taskcat.route.adsorption', {},
      '前往吸附能全流程', 'Open adsorption-energy workflow'), run: gotoAdsorption },
    spin_scan: { label: () => tr('runtime.taskcat.route.spin', {},
      '前往多自旋扫描', 'Open multi-spin scan'), run: gotoSpin },
    neb: { label: () => tr('runtime.taskcat.route.neb', {},
      '前往 NEB 面板', 'Open NEB panel'), run: expandNeb },
    formation_binding: { label: () => tr('runtime.taskcat.route.formation_binding', {},
      '前往形成能/结合能计算器', 'Open formation/binding-energy calculator'), run: gotoCalc },
    surface_energy: { label: () => tr('runtime.taskcat.route.surface_energy', {},
      '前往表面能计算器', 'Open surface-energy calculator'), run: gotoSurfaceEnergy },
  };
  const PARAMS = {
    eos: [{ k: 'scales', label: () => tr('runtime.taskcat.params.scales', {},
      '缩放因子(逗号分隔,留空=默认7点)', 'Scale factors (comma-separated; blank uses the default 7 points)'),
    ph: '0.96, 0.98, 1.0, 1.02, 1.04', list: 'float' }],
    bands: [{ k: 'lattice', label: () => tr('runtime.taskcat.params.lattice', {},
      '晶格类型', 'Lattice type'), sel: ['', 'fcc', 'bcc', 'hcp', 'sc', 'tet', 'orc'] },
    { k: 'npoints', label: () => tr('runtime.taskcat.params.kpoints_per_segment', {},
      '每段 k 点数', 'k-points per segment'), ph: '40', num: true }],
    chgdiff: [{ k: 'adsorbate_indices', label: () => tr(
      'runtime.taskcat.params.adsorbate_indices', {},
      '吸附质原子序号(1 起,逗号分隔;据此拆 AB/A/B)',
      'Adsorbate atom indices (1-based, comma-separated; used to split AB/A/B)'),
    ph: '27, 28', list: 'int' }],
    conv_encut: [{ k: 'values', label: () => tr('runtime.taskcat.params.encut_values', {},
      'ENCUT 值(逗号分隔)', 'ENCUT values (comma-separated)'),
    ph: '400, 450, 500, 550', list: 'int' }],
    conv_kmesh: [{ k: 'meshes', label: () => tr('runtime.taskcat.params.k_meshes', {},
      'k 网格(每行一个,如 3 3 1)', 'k-point meshes (one per line, e.g. 3 3 1)'),
    ph: '3 3 1\n5 5 1\n7 7 1', meshes: true }],
    conv_vacuum: [{ k: 'vacuums', label: () => tr('runtime.taskcat.params.vacuum', {},
      '真空厚度 Å(逗号分隔)', 'Vacuum thicknesses in Å (comma-separated)'),
    ph: '10, 12, 15, 18', list: 'float' }],
    conv_thickness: [{ k: 'layers', label: () => tr('runtime.taskcat.params.layers', {},
      '层数(逗号分隔)', 'Layer counts (comma-separated)'),
    ph: '3, 4, 5', list: 'int' }],
    aimd: [{ k: 'ensemble', label: () => tr('runtime.taskcat.params.ensemble', {},
      '系综', 'Ensemble'), sel: ['nvt', 'nve'] },
    { k: 'temp_k', label: () => tr('runtime.taskcat.params.temperature', {},
      '温度 K', 'Temperature K'), ph: '300', num: true },
    { k: 'steps', label: () => tr('runtime.taskcat.params.steps', {},
      '步数', 'Steps'), ph: '10000', num: true },
    { k: 'potim_fs', label: () => tr('runtime.taskcat.params.timestep', {},
      '步长 fs', 'Time step fs'), ph: '1.0', num: true }],
    dimer: [{ k: 'amplitude', label: () => tr('runtime.taskcat.params.displacement', {},
      '初始位移幅度 Å', 'Initial displacement amplitude Å'), ph: '0.01', num: true }],
    cellopt: [{ k: 'bump_encut', label: () => tr('runtime.taskcat.params.pulay_encut', {},
      'ENCUT×1.3(缓 Pulay 应力)', 'ENCUT×1.3 (reduce Pulay stress)'), check: true }],
    vaspsol: [{ k: 'eb_k', label: () => tr('runtime.taskcat.params.dielectric', {},
      '溶剂相对介电常数 EB_K', 'Solvent relative permittivity EB_K'),
    ph: '78.4', num: true }],
  };
  // 任务性质徽标底色(P2:作业生成 / 结果计算器 / INCAR 顾问)
  const BADGE_CLS = { '作业生成': 'gen', '结果计算器': 'calc', 'INCAR 顾问': 'advisor' };
  const BADGE_LABEL = {
    '作业生成': () => tr('runtime.taskcat.badge.job_generation', {},
      '作业生成', 'Job generation'),
    '结果计算器': () => tr('runtime.taskcat.badge.result_calculator', {},
      '结果计算器', 'Result calculator'),
    'INCAR 顾问': () => tr('runtime.taskcat.badge.incar_advisor', {},
      'INCAR 顾问', 'INCAR advisor'),
  };
  const ANALYSIS_LABEL = {
    integrated: () => tr('runtime.taskcat.analysis.integrated', {},
      '自动解析 + 报告', 'Automatic analysis + report'),
    evidence_only: () => tr('runtime.taskcat.analysis.evidence_only', {},
      '产物核对 + 报告', 'Output verification + report'),
    dedicated: () => tr('runtime.taskcat.analysis.dedicated', {},
      '专用流程 + 报告', 'Dedicated workflow + report'),
    unsupported: () => tr('runtime.taskcat.analysis.unsupported', {},
      '暂不支持分析', 'Analysis is not supported yet'),
  };

  // ── ② 计算类型目录 ──
  async function loadCatalog() {
    const sceneKey = (VCS.scenario && VCS.scenario.key) || null;
    const active = VCS.activeCalculation || null;
    const r = await VCS.call('task_catalog', sceneKey, active, VCS.activeEngine || 'vasp');
    if (!r || r.ok === false) {
      VCS.log(tr('runtime.taskcat.catalog.load_failed', {
        error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
      }, '加载计算类型目录失败:{error}', 'Failed to load the calculation catalog: {error}'), 'failc');
      return;
    }
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
    let html = `<option value="">${VCS.esc(tr(
      'runtime.taskcat.catalog.select', {}, '选择计算类型…', 'Select calculation type…',
    ))}</option>`;
    cats.forEach(c => {
      const items = State.tasks.filter(t => t.category === c);
      if (!items.length) return;
      const category = uiEnglish()
        ? ((items[0] && items[0].category_en) || c) : c;
      html += `<optgroup label="${VCS.esc(category)}">` + items.map(t =>
        `<option value="${VCS.esc(t.key)}"${State.sel === t.key ? ' selected' : ''}>` +
        `${VCS.esc(taskText(t, 'name_zh', 'name_en'))}${DERIVABLE.has(t.key) ? '' : tr(
          'runtime.taskcat.catalog.dedicated_suffix', {}, '(专用流程)', ' (dedicated workflow)',
        )}</option>`).join('') + '</optgroup>';
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
  function renderTaskHeader(t, key) {
    if (!t) return;
    if ($('tc-sel-name')) $('tc-sel-name').textContent = taskText(t, 'name_zh', 'name_en');
    if ($('tc-sel-desc')) $('tc-sel-desc').textContent = taskText(t, 'description', 'description_en');
    const kind = t.kind_badge || '作业生成';
    const routed = !!ROUTED[key];
    const kindLabel = BADGE_LABEL[kind] ? BADGE_LABEL[kind]()
      : (uiEnglish() ? (t.kind_badge_en || kind) : kind);
    if ($('tc-sel-badges')) $('tc-sel-badges').innerHTML =
      `<span class="tc-badge tc-badge-${BADGE_CLS[kind] || 'gen'}">${VCS.esc(kindLabel)}</span>` +
      `<span class="tc-badge">${VCS.esc(ANALYSIS_LABEL[t.analysis_status]
        ? ANALYSIS_LABEL[t.analysis_status]() : tr(
          'runtime.taskcat.analysis.pending', {}, '分析能力待确认', 'Analysis capability pending confirmation'))}</span>` +
      `<span class="tc-badge">${VCS.esc(tr('runtime.taskcat.catalog.outputs', {
        outputs: taskText(t, 'outputs', 'outputs_en') || '—',
      }, '产物:{outputs}', 'Outputs: {outputs}'))}</span>` +
      (DERIVABLE.has(key) || routed ? '' : `<span class="tc-badge">${VCS.esc(tr(
        'runtime.taskcat.catalog.dedicated_only', {},
        '此类型经专用流程建作业(非一键派生)',
        'This type creates jobs through a dedicated workflow, not one-click derivation',
      ))}</span>`);
    const dbtn = $('tc-derive-btn');
    if (dbtn) {
      dbtn.disabled = !DERIVABLE.has(key) && !routed;
      dbtn.textContent = routed ? ROUTED[key].label() : tr(
        'runtime.taskcat.derive.button', {}, '派生作业', 'Derive jobs');
    }
  }
  function selectTask(key) {
    State.sel = key;
    State.uSugg = null;
    renderGrid();
    const t = State.tasks.find(x => x.key === key);
    const form = $('tc-form');
    if (!t || !form) return;
    form.hidden = false;
    renderTaskHeader(t, key);
    renderParams(key);
    const ubox = $('tc-u-box'); if (ubox) ubox.hidden = false;    // DFT+U 建议表(可选助手,始终可用)
    const uout = $('tc-u-out'); if (uout) uout.innerHTML = '';
    const uapply = $('tc-u-apply'); if (uapply) uapply.hidden = true;
  }
  // 跳转/展开专用面板(替代 neb / formation_binding 的禁用死按钮)
  function expandNeb() {
    const card = document.getElementById('neb-card');
    if (card) {
      if (VCS.ui && typeof VCS.ui.setAccordionOpen === 'function') {
        VCS.ui.setAccordionOpen(card, true, true);
      } else {
        card.setAttribute('data-open', '1');
        const toggle = card.querySelector(':scope > .acc-h > .acc-toggle');
        if (toggle) toggle.setAttribute('aria-expanded', 'true');
      }
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    VCS.toast(tr('runtime.taskcat.route.neb_opened', {},
      '已展开 NEB 面板:填始态/末态目录后「生成 NEB」',
      'NEB panel expanded; select initial/final-state directories, then generate NEB'));
  }
  function focusAfterNavigate(page, selector, message) {
    VCS.navigate(page, { source: 'task-catalog', focusSelector: selector }).then(out => {
      if (out.ok && message) VCS.toast(message);
    });
  }
  function gotoRelax() {
    focusAfterNavigate('generate', '#gen-poscar', tr(
      'runtime.taskcat.route.relax_hint', {},
      '选择 POSCAR、INCAR 和输出目录后生成四件套',
      'Select POSCAR, INCAR, and an output directory to generate the four-file input set'));
  }
  function gotoAdsorption() {
    focusAfterNavigate('project', '#ads-journey', tr(
      'runtime.taskcat.route.adsorption_opened', {},
      '已进入吸附能全流程', 'Adsorption-energy workflow opened'));
  }
  function gotoSpin() {
    focusAfterNavigate('structure', '#spin-card', tr(
      'runtime.taskcat.route.spin_opened', {},
      '已定位多自旋态扫描', 'Multi-spin-state scan located'));
  }
  function gotoCalc() {
    const nav = document.querySelector('nav a[data-page="project"]');
    if (nav) nav.click();
    setTimeout(() => {
      const c = document.getElementById('fb-card');
      if (c) c.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 120);
    VCS.toast(tr('runtime.taskcat.route.formation_binding_opened', {},
      '已跳转到结果分析页的形成能/结合能计算器',
      'Formation/binding-energy calculator opened on Results Analysis'));
  }
  function gotoSurfaceEnergy() {
    const ana = $('analysis-type');
    if (ana) { ana.value = 'taskana'; ana.dispatchEvent(new Event('change', { bubbles: true })); }
    focusAfterNavigate('project', '#ta-se-slab', tr(
      'runtime.taskcat.route.surface_energy_hint', {},
      '选择 slab 与同口径 bulk 作业后计算表面能',
      'Select slab and method-matched bulk jobs, then calculate surface energy'));
  }
  function renderParams(key) {
    const box = $('tc-params');
    if (!box) return;
    const schema = PARAMS[key] || [];
    box.innerHTML = schema.map(f => {
      const label = typeof f.label === 'function' ? f.label() : f.label;
      if (f.check) return `<label class="gen-lbl" style="display:flex;gap:6px;align-items:center;margin-bottom:8px">` +
        `<input type="checkbox" id="tcp-${f.k}" checked> ${VCS.esc(label)}</label>`;
      let field;
      if (f.sel) field = `<select id="tcp-${f.k}" class="ipt">` +
        f.sel.map(o => `<option value="${VCS.esc(o)}">${VCS.esc(o || tr(
          'runtime.taskcat.params.automatic', {}, '自动', 'Automatic',
        ))}</option>`).join('') + '</select>';
      else if (f.meshes) field = `<textarea id="tcp-${f.k}" class="ipt" rows="3" placeholder="${VCS.esc(f.ph)}"></textarea>`;
      else field = `<input id="tcp-${f.k}" class="ipt" placeholder="${VCS.esc(f.ph || '')}">`;
      return `<div class="gen-fld"><span class="gen-lbl">${VCS.esc(label)}</span>${field}</div>`;
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
    if (!els.length) {
      VCS.log(tr('runtime.taskcat.u.elements_required', {},
        '请填元素符号', 'Enter element symbols'), 'failc'); return;
    }
    const r = await VCS.call('u_suggest', els);
    const out = $('tc-u-out');
    if (!r || r.ok === false) {
      if (out) out.innerHTML = `<span class="sub">${VCS.esc(tr(
        'runtime.taskcat.u.query_failed', { error: (r && r.error) || '' },
        '查询失败:{error}', 'Query failed: {error}',
      ))}</span>`;
      return;
    }
    State.uSugg = r;
    let h = (r.suggestions || []).map(s =>
      `<div class="urow"><b>${VCS.esc(s.element)}</b> U=${s.u} eV(${VCS.esc(s.orbital)})· ${VCS.esc(s.source)}` +
      `<span class="sub" title="${VCS.esc(s.note)}"> ⓘ</span></div>`).join('');
    if ((r.missing || []).length) h += `<div class="sub" style="color:var(--warn)">${VCS.esc(tr(
      'runtime.taskcat.u.missing', { elements: (r.missing).join(', ') },
      '库内无经验 U(不编造):{elements}',
      'No empirical U in the library; no value was fabricated: {elements}',
    ))}</div>`;
    if (out) out.innerHTML = h || `<span class="sub">${VCS.esc(tr(
      'runtime.taskcat.u.no_matches', {}, '无匹配元素', 'No matching elements',
    ))}</span>`;
    const apply = $('tc-u-apply');
    if (apply) apply.hidden = !(r.suggestions || []).length;
  }
  function applyU() {
    if (!State.uSugg || !State.uSugg.incar_keys) return;
    const keys = State.uSugg.incar_keys;
    const lines = Object.keys(keys).map(k => `${k} = ${keys[k] === true ? '.TRUE.' : keys[k]}`).join('\n');
    const kw = $('gen-extra-kw');
    if (kw) {
      kw.value = (kw.value ? kw.value + '\n' : '') + lines;
      VCS.toast(tr('runtime.taskcat.u.applied_to_keywords', {},
        '已把 LDAU 键追加到②自定义关键词',
        'LDAU keys appended to Custom keywords in Input Generation'));
    }
    VCS.log(tr('runtime.taskcat.u.applied', {},
      'DFT+U:已把 LDAU 系列键写入生成页自定义关键词',
      'DFT+U: LDAU keys were added to the custom keywords on Input Generation'), 'okc');
  }
  async function derive() {
    const key = State.sel;
    if (!key) {
      VCS.log(tr('runtime.taskcat.derive.task_required', {},
        '请先选择计算类型', 'Select a calculation type first'), 'failc'); return;
    }
    if (ROUTED[key]) { ROUTED[key].run(); return; }       // 专用面板:跳转/展开,不走一键派生
    const src = (($('tc-src') && $('tc-src').value) || '').trim();
    if (!DERIVABLE.has(key)) {
      VCS.log(tr('runtime.taskcat.derive.dedicated_only', {},
        '该类型经专用流程建作业,非一键派生',
        'This type creates jobs through a dedicated workflow, not one-click derivation'), 'failc'); return;
    }
    if (!src) {
      VCS.log(tr('runtime.taskcat.derive.source_required', {},
        '请填源作业目录', 'Enter a source job directory'), 'failc'); return;
    }
    const btn = $('tc-derive-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.taskcat.derive.running', { task: key },
      '派生作业({task})…', 'Deriving jobs ({task})…'));
    try {
      const r = await VCS.call('derive_task', key, src, collectParams(key));
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.taskcat.derive.failed', {
          error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
        }, '派生失败:{error}', 'Derivation failed: {error}'), 'failc'); return;
      }
      const n = (r.job_dirs || []).length;
      // 诚实化:0 作业 + note(如层厚收敛需从建 slab 流程发起)→ warn 级说明,绝不报成功 toast。
      if (n === 0 && r.note) {
        VCS.log(tr('runtime.taskcat.derive.none_created', { note: r.note },
          '未生成作业:{note}', 'No jobs were generated: {note}'), 'warnc');
        (r.warnings || []).forEach(w => {
          if (w !== r.note) VCS.log(tr('runtime.taskcat.common.note', { note: w },
            '提示:{note}', 'Note: {note}'), 'warnc');
        });
        VCS.toast(tr('runtime.taskcat.derive.none_toast', {},
          '未生成作业(见日志说明)', 'No jobs were generated; see the log for details'));
        return;
      }
      VCS.log(tr('runtime.taskcat.derive.created', {
        count: n, jobs: (r.job_dirs || []).map(d => d.split(/[\\/]/).pop()).join(', '),
      }, '已派生 {count} 个作业并入台账:{jobs}',
      'Derived and registered {count} jobs: {jobs}'), 'okc');
      (r.warnings || []).forEach(w => VCS.log(tr(
        'runtime.taskcat.common.note', { note: w }, '提示:{note}', 'Note: {note}'), 'warnc'));
      VCS.toast(tr('runtime.taskcat.derive.toast_created', { count: n },
        '已派生 {count} 个作业', 'Derived {count} jobs'));
      if (n && VCS.nextStep) VCS.nextStep({
        title: tr('runtime.taskcat.derive.next_step_title', {},
          '派生作业已生成', 'Derived jobs generated'),
        message: tr('runtime.taskcat.derive.next_step_message', { count: n },
          '已创建 {count} 个作业并加入任务列表。',
          'Created {count} jobs and added them to the job list.'),
        detail: tr('runtime.taskcat.derive.next_step_detail', {},
          '下一步：选择服务器、核数与墙时，然后上传并提交。',
          'Next: select a server, core count, and wall time, then upload and submit.'),
        primaryLabel: tr('runtime.taskcat.common.open_jobs_submit', {},
          '前往任务页提交', 'Open Jobs to submit'), page: 'jobs', focusJobDir: (r.job_dirs || [])[0] || '',
      });
    } finally { if (btn) btn.disabled = false; }
  }

  // ── NEB 面板(P0-1):始/末态目录 + image 数 → derive_neb ──
  async function generateNeb() {
    const s = (($('neb-start') && $('neb-start').value) || '').trim();
    const e = (($('neb-end') && $('neb-end').value) || '').trim();
    const nimg = parseInt(($('neb-nimages') && $('neb-nimages').value) || '5', 10) || 5;
    if (!s) {
      VCS.log(tr('runtime.taskcat.neb.start_required', {},
        'NEB:请选始态作业目录', 'NEB: select the initial-state job directory'), 'failc'); return;
    }
    if (!e) {
      VCS.log(tr('runtime.taskcat.neb.end_required', {},
        'NEB:请选末态作业目录', 'NEB: select the final-state job directory'), 'failc'); return;
    }
    const btn = $('neb-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.taskcat.neb.generating', { images: nimg },
      '生成 NEB 目录树(始态+末态,{images} 个中间 image)…',
      'Generating the NEB directory tree (initial + final state, {images} intermediate images)…'));
    try {
      const r = await VCS.call('derive_neb', s, e, nimg);
      const out = $('neb-out');
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.taskcat.neb.failed', {
          error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
        }, 'NEB 生成失败:{error}', 'NEB generation failed: {error}'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc(
          (r && r.error) || tr('runtime.taskcat.neb.failed_short', {},
            'NEB 生成失败', 'NEB generation failed'))}</div>`;
        return;
      }
      (r.changes || []).forEach(c => VCS.log(c, 'okc'));
      (r.warnings || []).forEach(w => VCS.log(tr(
        'runtime.taskcat.common.note', { note: w }, '提示:{note}', 'Note: {note}'), 'warnc'));
      VCS.log(tr('runtime.taskcat.neb.registered', { path: r.job_dir },
        'NEB 目录树已生成并入台账:{path}',
        'NEB directory tree generated and registered: {path}'), 'okc');
      if (out) out.innerHTML = `<div class="ta-summary">${VCS.esc(tr(
        'runtime.taskcat.neb.summary', { path: r.job_dir, images: r.n_images },
        '已生成 NEB 目录:{path} · 00 初态 + {images} 中间 image + 末态',
        'NEB directory generated: {path} · 00 initial state + {images} intermediate images + final state',
      ))}<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.job_dir)}">${VCS.esc(tr(
        'runtime.taskcat.common.open_directory', {}, '打开目录', 'Open directory',
      ))}</button></div></div>`;
      VCS.toast(tr('runtime.taskcat.neb.complete', {},
        '已生成 NEB 目录树', 'NEB directory tree generated'));
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      if (VCS.nextStep) VCS.nextStep({
        title: tr('runtime.taskcat.neb.next_step_title', {},
          'NEB 作业已生成', 'NEB job generated'),
        message: tr('runtime.taskcat.neb.next_step_message', {},
          'NEB 目录树已加入任务列表。', 'The NEB directory tree was added to the job list.'),
        detail: tr('runtime.taskcat.neb.next_step_detail', {},
          '下一步：选择服务器并上传提交；任务页会按 NEB 目录结构处理。',
          'Next: select a server, upload, and submit. Jobs will handle the NEB directory structure.'),
        primaryLabel: tr('runtime.taskcat.common.open_jobs_submit', {},
          '前往任务页提交', 'Open Jobs to submit'), page: 'jobs', focusJobDir: r.job_dir,
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
    if (!sac) {
      VCS.log(tr('runtime.taskcat.formation.sac_required', {},
        '形成能/结合能:请选 SAC 作业目录',
        'Formation/binding energy: select an SAC job directory'), 'failc'); return;
    }
    VCS.log(tr('runtime.taskcat.formation.calculating', {},
      '形成能/结合能计算…', 'Calculating formation/binding energy…'));
    const r = await VCS.call('formation_binding_calc', sac, sub || null,
      Object.keys(atom).length ? atom : null, Object.keys(mu).length ? mu : null);
    const out = $('fb-out');
    if (!r || r.error) {
      (r && r.warnings || []).forEach(warning => VCS.log(tr(
        'runtime.taskcat.formation.warning', { warning },
        '形成能/结合能:{warning}', 'Formation/binding energy: {warning}'), 'warnc'));
      VCS.log(tr('runtime.taskcat.common.calculation_failed', {
        error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
      }, '计算失败:{error}', 'Calculation failed: {error}'), 'failc');
      const warningHtml = (r && r.warnings || []).map(warning =>
        `<div class="sub" style="color:var(--warn);margin-top:4px">⚠ ${VCS.esc(warning)}</div>`).join('');
      if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc(
        (r && r.error) || tr('runtime.taskcat.common.calculation_failed_short', {},
          '计算失败', 'Calculation failed'))}${warningHtml}</div>`;
      return; }
    let h = '<div class="ta-summary">';
    if (r.e_sac != null) h += `E(SAC)=${(+r.e_sac).toFixed(4)} eV`;
    if (r.e_substrate != null) h += tr('runtime.taskcat.formation.substrate_energy', {
      energy: (+r.e_substrate).toFixed(4),
    }, ' · E(基底)={energy} eV', ' · E(substrate)={energy} eV');
    if (r.metal) h += tr('runtime.taskcat.formation.metal', { metal: VCS.esc(r.metal) },
      ' · 金属=<b>{metal}</b>', ' · Metal=<b>{metal}</b>');
    if (r.binding_energy != null) h += tr('runtime.taskcat.formation.binding_energy', {
      energy: (+r.binding_energy).toFixed(4),
    }, '<br>结合能 <b>Eb = {energy} eV</b>', '<br>Binding energy <b>Eb = {energy} eV</b>');
    if (r.sigma != null) h += tr('runtime.taskcat.formation.stability', {
      sigma: (+r.sigma).toFixed(3), state: r.stable
        ? tr('runtime.taskcat.formation.stable', {}, '判稳', 'stable')
        : tr('runtime.taskcat.formation.unstable', {}, '判不稳', 'unstable'),
    }, ' · σ = {sigma}({state})', ' · σ = {sigma} ({state})');
    if (r.formation_energy != null) h += tr('runtime.taskcat.formation.formation_energy', {
      energy: (+r.formation_energy).toFixed(4),
    }, '<br>形成能 <b>Ef = {energy} eV</b>', '<br>Formation energy <b>Ef = {energy} eV</b>');
    if (r.stability_note) h += `<div class="sub" style="margin-top:4px">${VCS.esc(r.stability_note)}</div>`;
    (r.hints || []).forEach(hint => { h += `<div class="sub" style="color:var(--warn);margin-top:4px">${VCS.esc(hint)}</div>`; });
    (r.warnings || []).forEach(warning => {
      h += `<div class="sub" style="color:var(--warn);margin-top:4px">⚠ ${VCS.esc(warning)}</div>`;
      VCS.log(tr('runtime.taskcat.formation.warning', { warning },
        '形成能/结合能:{warning}', 'Formation/binding energy: {warning}'), 'warnc');
    });
    if (r.report_file) h += `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.report_file)}">${VCS.esc(tr(
      'runtime.taskcat.common.open_traceable_report', {},
      '打开可追溯报告', 'Open traceable report',
    ))}</button></div>`;
    h += '</div>';
    if (out) out.innerHTML = h;
    if (r.binding_energy != null || r.formation_energy != null)
      VCS.log(tr('runtime.taskcat.formation.complete', {},
        '形成能/结合能计算完成', 'Formation/binding-energy calculation complete'), 'okc');
    else VCS.log(tr('runtime.taskcat.formation.incomplete', {},
      '缺输入,未算出 Eb/Ef(见提示)',
      'Input is missing; Eb/Ef was not calculated (see notes)'), 'warnc');
  }

  // ── 差分电荷合成(P0-4)──
  async function chgdiffSynth() {
    const ab = (($('cd-ab') && $('cd-ab').value) || '').trim();
    const a = (($('cd-a') && $('cd-a').value) || '').trim();
    const b = (($('cd-b') && $('cd-b').value) || '').trim();
    if (!ab || !a || !b) {
      VCS.log(tr('runtime.taskcat.chgdiff.jobs_required', {},
        '差分电荷:请选 AB / A / B 三个作业目录',
        'Charge-density difference: select the AB, A, and B job directories'), 'failc'); return;
    }
    const btn = $('cd-run');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.taskcat.chgdiff.calculating', {},
      '差分电荷合成 Δρ=ρ(AB)−ρ(A)−ρ(B)…',
      'Computing charge-density difference Δρ=ρ(AB)−ρ(A)−ρ(B)…'));
    try {
      const r = await VCS.call('compute_chgdiff', ab, a, b, null);
      const out = $('cd-out');
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.taskcat.chgdiff.failed', {
          error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
        }, '差分电荷合成失败:{error}', 'Charge-density-difference synthesis failed: {error}'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc(
          (r && r.error) || tr('runtime.taskcat.chgdiff.failed_short', {},
            '合成失败', 'Synthesis failed'))}</div>`;
        return;
      }
      let h = `<div class="ta-summary">${tr('runtime.taskcat.chgdiff.summary', {
        path: VCS.esc(r.out), maximum: (+r.max).toExponential(3),
        minimum: (+r.min).toExponential(3), grid: r.n_grid,
      }, '已合成 <b>{path}</b>(VESTA 可读) · Δ(ρ×V) 极大 {maximum} / 极小 {minimum} · 网格 {grid}',
      'Synthesized <b>{path}</b> (VESTA-readable) · Δ(ρ×V) maximum {maximum} / minimum {minimum} · grid {grid}')}` +
        `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.out)}">${VCS.esc(tr(
          'runtime.taskcat.common.open_directory', {}, '打开目录', 'Open directory',
        ))}</button>` +
        (r.report_file ? `<button class="btn" data-open="${VCS.esc(r.report_file)}">${VCS.esc(tr(
          'runtime.taskcat.common.open_traceable_report', {},
          '打开可追溯报告', 'Open traceable report',
        ))}</button>` : '') +
        `</div>` + (r.warnings || []).map(w => `<div class="sub" style="color:var(--warn)">⚠ ${VCS.esc(w)}</div>`).join('') + `</div>`;
      if (out) {
        out.innerHTML = h + '<div id="cd-chart" style="height:220px;margin-top:8px"></div>';
        renderChargeProfile(r.profile || {});
      }
      VCS.log(tr('runtime.taskcat.chgdiff.complete_path', { path: r.out },
        '差分电荷合成完成:{path}', 'Charge-density difference complete: {path}'), 'okc');
      (r.warnings || []).forEach(w => VCS.log(tr(
        'runtime.taskcat.chgdiff.warning', { warning: w },
        '差分电荷:{warning}', 'Charge-density difference: {warning}'), 'warnc'));
      VCS.toast(tr('runtime.taskcat.chgdiff.complete', {},
        '已合成差分电荷 Δρ', 'Charge-density difference Δρ synthesized'));
    } finally { if (btn) btn.disabled = false; }
  }
  function renderChargeProfile(profile) {
    const el = document.getElementById('cd-chart');
    if (!el) return;
    const z = profile.z || []; const rho = profile.rho || [];
    if (!z.length || typeof echarts === 'undefined') {
      el.textContent = z.length
        ? tr('runtime.taskcat.chgdiff.echarts_unavailable', {},
          '(echarts 未加载)', '(echarts is not loaded)')
        : tr('runtime.taskcat.chgdiff.no_planar_average', {},
          '(无面平均数据)', '(No planar-average data)');
      return;
    }
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
    option.textContent = tr('runtime.taskcat.analysis.from_settings', {
      task: task ? taskText(task, 'name_zh', 'name_en') : active,
    }, '按设置：{task}', 'From Settings: {task}');
    sel.insertBefore(option, sel.children[1] || null);
    sel.value = active;
  }

  async function analyze() {
    const d = (($('ta-dir') && $('ta-dir').value) || '').trim();
    if (!d) {
      VCS.log(tr('runtime.taskcat.analysis.directory_required', {},
        '请选择作业目录', 'Select a job directory'), 'failc'); return;
    }
    const kind = selectedAnalysisKind();
    const btn = $('ta-run');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.taskcat.analysis.running', {},
      '任务解析…', 'Analyzing task…'));
    try {
      const r = await VCS.call('analyze_task', d, kind || null);
      const out = $('ta-out');
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.taskcat.analysis.failed', {
          error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
        }, '解析失败:{error}', 'Analysis failed: {error}'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc(
          (r && r.error) || tr('runtime.taskcat.analysis.failed_short', {},
            '解析失败', 'Analysis failed'))}` +
          `${r && r.next_action ? `<div class="sub" style="margin-top:6px">${VCS.esc(tr(
            'runtime.taskcat.analysis.next_action', { action: r.next_action },
            '下一步：{action}', 'Next: {action}',
          ))}</div>` : ''}</div>`;
        return;
      }
      let h = `<div class="ta-summary"><b>${VCS.esc(r.kind)}</b> — ${VCS.esc(r.summary || '')}` +
        `${r.next_action ? `<div class="sub" style="margin-top:6px">${VCS.esc(tr(
          'runtime.taskcat.analysis.recommendation', { action: r.next_action },
          '建议：{action}', 'Recommendation: {action}',
        ))}</div>` : ''}</div>`;
      if (r.figure) h += `<img class="ta-img" src="file://${VCS.esc(r.figure)}" alt="figure">` +
        `<div class="actions" style="margin-top:6px"><button class="btn" data-open="${VCS.esc(r.figure)}">${VCS.esc(tr(
          'runtime.taskcat.common.open_directory', {}, '打开目录', 'Open directory',
        ))}</button></div>`;
      if (out) out.innerHTML = h;
      VCS.log(tr('runtime.taskcat.analysis.complete', { summary: r.summary || r.kind },
        '解析完成:{summary}', 'Analysis complete: {summary}'), 'okc');
    } finally { if (btn) btn.disabled = false; }
  }

  async function makeTaskReport() {
    const d = (($('ta-dir') && $('ta-dir').value) || '').trim();
    if (!d) {
      VCS.log(tr('runtime.taskcat.report.directory_required', {},
        '生成报告前请先选择作业目录',
        'Select a job directory before generating a report'), 'failc'); return;
    }
    const sep = d.includes('\\') ? '\\' : '/';
    const path = d.replace(/[\\/]$/, '') + sep + 'vcstudio-task-report.html';
    const btn = $('ta-report'); if (btn) btn.disabled = true;
    try {
      const r = await VCS.call('task_report', d, path, selectedAnalysisKind() || null);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.taskcat.report.failed', {
          error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
        }, '报告生成失败:{error}', 'Report generation failed: {error}'), 'failc'); return;
      }
      VCS.log(tr('runtime.taskcat.report.created', { file: r.file },
        '可追溯报告已生成:{file}', 'Traceable report generated: {file}'),
      r.analysis_ok ? 'okc' : 'warnc');
      VCS.nextStep({ title: tr('runtime.taskcat.report.next_step_title', {},
        '任务报告已生成', 'Task report generated'),
      message: r.analysis_ok ? tr('runtime.taskcat.report.success_message', {},
        '解析结果和文件指纹已写入报告。',
        'Analysis results and file fingerprints were written to the report.') : tr(
        'runtime.taskcat.report.blocked_message', {},
        '报告已记录缺失项和下一步，没有伪造分析结论。',
        'The report records missing items and next steps without fabricating analytical conclusions.'),
      detail: r.file, primaryLabel: tr('runtime.taskcat.report.open_directory', {},
        '打开报告所在目录', 'Open report directory'),
        onPrimary: () => VCS.call('open_dir', r.file) });
    } finally { if (btn) btn.disabled = false; }
  }
  async function surfEnergy() {
    const slab = (($('ta-se-slab') && $('ta-se-slab').value) || '').trim();
    const bulk = (($('ta-se-bulk') && $('ta-se-bulk').value) || '').trim();
    const eb = (($('ta-se-ebulk') && $('ta-se-ebulk').value) || '').trim();
    if (!slab) {
      VCS.log(tr('runtime.taskcat.surface_energy.slab_required', {},
        '请选 slab 作业', 'Select a slab job'), 'failc'); return;
    }
    VCS.log(tr('runtime.taskcat.surface_energy.calculating', {},
      '表面能计算…', 'Calculating surface energy…'));
    const r = await VCS.call('surface_energy_calc', slab, bulk, eb ? parseFloat(eb) : null, null);
    const out = $('ta-out');
    if (!r || r.ok === false || r.error) {
      (r && r.warnings || []).forEach(warning => VCS.log(tr(
        'runtime.taskcat.surface_energy.warning', { warning },
        '表面能:{warning}', 'Surface energy: {warning}'), 'warnc'));
      VCS.log(tr('runtime.taskcat.surface_energy.failed', {
        error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
      }, '表面能计算失败:{error}', 'Surface-energy calculation failed: {error}'), 'failc'); return;
    }
    const warnings = (r.warnings || []).map(warning =>
      `<div class="sub" style="color:var(--warn);margin-top:4px">⚠ ${VCS.esc(warning)}</div>`).join('');
    const report = r.report_file
      ? `<div class="actions"><button class="btn" data-open="${VCS.esc(r.report_file)}">${VCS.esc(tr(
        'runtime.taskcat.common.open_traceable_report', {},
        '打开可追溯报告', 'Open traceable report',
      ))}</button></div>` : '';
    if (out) out.innerHTML = `<div class="ta-summary">${tr(
      'runtime.taskcat.surface_energy.summary', {
        gamma: r.gamma_jm2.toFixed(4), area: r.area_a2.toFixed(2),
        count: r.n_slab, bulk: r.e_bulk_per_atom.toFixed(4),
      }, '表面能 <b>γ = {gamma} J/m²</b> · 面积 {area} Å² · N={count} · E_bulk/atom={bulk} eV',
      'Surface energy <b>γ = {gamma} J/m²</b> · area {area} Å² · N={count} · E_bulk/atom={bulk} eV')}${warnings}${report}</div>`;
    (r.warnings || []).forEach(warning => VCS.log(tr(
      'runtime.taskcat.surface_energy.warning', { warning },
      '表面能:{warning}', 'Surface energy: {warning}'), 'warnc'));
    VCS.log('γ = ' + r.gamma_jm2.toFixed(4) + ' J/m²', 'okc');
  }

  async function vaspsolPair() {
    const vacuum = (($('vs-vacuum') && $('vs-vacuum').value) || '').trim();
    const solvent = (($('vs-solvent') && $('vs-solvent').value) || '').trim();
    if (!vacuum || !solvent) {
      VCS.log(tr('runtime.taskcat.vaspsol.jobs_required', {},
        'VASPsol 配对：请选择真空与溶剂两个作业目录',
        'VASPsol pairing: select vacuum and solvent job directories'), 'failc'); return;
    }
    const btn = $('vs-run'); if (btn) btn.disabled = true;
    try {
      const r = await VCS.call('vaspsol_pair_calc', vacuum, solvent, null);
      const out = $('vs-out');
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.taskcat.vaspsol.failed', {
          error: (r && r.error) || tr('runtime.taskcat.common.unknown', {}, '未知', 'Unknown'),
        }, 'VASPsol 配对计算失败:{error}', 'VASPsol paired calculation failed: {error}'), 'failc');
        if (out) out.innerHTML = `<div class="ta-summary" style="color:var(--fail)">${VCS.esc(
          (r && r.error) || tr('runtime.taskcat.common.calculation_failed_short', {},
            '计算失败', 'Calculation failed'))}</div>`;
        return;
      }
      if (out) out.innerHTML = `<div class="ta-summary">${tr(
        'runtime.taskcat.vaspsol.summary', {
          solvation: (+r.solvation_energy_eV).toFixed(8),
          vacuum: (+r.e_vacuum_eV).toFixed(8), solvent: (+r.e_solvent_eV).toFixed(8),
        }, '溶剂化能 <b>ΔE<sub>solv</sub> = {solvation} eV</b> · E<sub>vac</sub>={vacuum} eV · E<sub>sol</sub>={solvent} eV',
        'Solvation energy <b>ΔE<sub>solv</sub> = {solvation} eV</b> · E<sub>vac</sub>={vacuum} eV · E<sub>sol</sub>={solvent} eV')}` +
        `<div class="sub">${VCS.esc(tr('runtime.taskcat.vaspsol.verified', {
          report: r.report_file,
        }, '已验证配对、方法一致性与 VASPsol OUTCAR 证据；报告：{report}',
        'Pairing, method consistency, and VASPsol OUTCAR evidence verified; report: {report}'))}</div>` +
        `<div class="actions"><button class="btn" data-open="${VCS.esc(r.report_file)}">${VCS.esc(tr(
          'runtime.taskcat.report.open_directory', {},
          '打开报告所在目录', 'Open report directory',
        ))}</button></div></div>`;
      VCS.log(tr('runtime.taskcat.vaspsol.report_created', { file: r.report_file },
        'VASPsol 配对报告已生成:{file}', 'VASPsol pairing report generated: {file}'), 'okc');
    } finally { if (btn) btn.disabled = false; }
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  async function browseInto(id, kind) {
    const r = await VCS.call(kind === 'dir' ? 'pick_dir' : 'pick_file', kind === 'dir' ? undefined : kind);
    if (r && r.path && $(id)) $(id).value = r.path;
  }
  function redrawLanguage() {
    renderCats();
    renderGrid();
    const task = State.tasks.find(row => row.key === State.sel);
    if (task) renderTaskHeader(task, State.sel);
    (PARAMS[State.sel] || []).forEach(field => {
      const input = $('tcp-' + field.k);
      if (!input) return;
      const label = typeof field.label === 'function' ? field.label() : field.label;
      if (field.check) {
        const host = input.closest('label');
        if (host) {
          const textNode = Array.from(host.childNodes).find(node => node.nodeType === Node.TEXT_NODE);
          if (textNode) textNode.nodeValue = ' ' + label;
        }
      } else {
        const host = input.closest('.gen-fld');
        const target = host && host.querySelector('.gen-lbl');
        if (target) target.textContent = label;
        if (field.sel) {
          const automatic = Array.from(input.options).find(option => option.value === '');
          if (automatic) automatic.textContent = tr(
            'runtime.taskcat.params.automatic', {}, '自动', 'Automatic');
        }
      }
    });
    syncAnalysisKind();
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
  document.addEventListener('vcs:language', redrawLanguage);
  window.TaskCat = { reload: loadCatalog };
})();
