// gaussmol.js — ②生成输入·Gaussian 分子面板(engine=gaussian 时展开)。
// 任务/泛函/基组/色散/溶剂/混合基组/电荷·多重度/资源 → 组装 CalcSpec extras,经 engine_preview
// (实时预览)与 engine_generate(落盘)出 .gjf。结构来源:①分子建模「下一步」带入的分子,或
// 选结构文件。混合基组走周期表弹窗。只依赖 app.js 的 VCS.*;插值 pre 用 textContent。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };

  const State = { molStruct: null, mixed: null, previewText: '' };
  const CALCULATION_TASK = { relax: 'opt', static: 'sp', freq: 'freq' };

  // 结构 → POSCAR 文本(分子装 15Å 盒;species 分块,供后端 parse_structure 读取笛卡尔坐标)
  function poscarText(st, comment) {
    const els = st.elements, coords = st.coords;
    const uniq = [];
    els.forEach(e => { if (uniq.indexOf(e) < 0) uniq.push(e); });
    const counts = uniq.map(u => els.filter(e => e === u).length);
    const lat = st.lattice && st.lattice.length === 3 ? st.lattice
      : [[15, 0, 0], [0, 15, 0], [0, 0, 15]];
    const lines = [comment || 'molecule', '1.0'];
    lat.forEach(r => lines.push('  ' + r.slice(0, 3).map(x => (+x).toFixed(8)).join(' ')));
    lines.push('  ' + uniq.join(' '));
    lines.push('  ' + counts.join(' '));
    lines.push('Cartesian');
    uniq.forEach(u => {
      for (let i = 0; i < els.length; i++) {
        if (els[i] === u) {
          const c = coords[i];
          lines.push('  ' + (+c[0]).toFixed(8) + ' ' + (+c[1]).toFixed(8) + ' ' + (+c[2]).toFixed(8));
        }
      }
    });
    return lines.join('\n') + '\n';
  }

  // ── 任务下拉:gauss_tasks 填充 + 条件参数显隐 ──
  async function loadTasks() {
    const sel = $('gauss-task');
    if (!sel) return;
    const r = await VCS.call('gauss_tasks');
    const tasks = (r && r.tasks) || [];
    sel.innerHTML = tasks.map(t =>
      `<option value="${VCS.esc(t.key)}" title="${VCS.esc(t.note)}">${VCS.esc(t.name)}</option>`).join('');
    syncCalculationTask();
    onTaskChange();
  }
  function syncCalculationTask() {
    if (VCS.activeEngine !== 'gaussian') return;
    const sel = $('gauss-task');
    const target = CALCULATION_TASK[VCS.activeCalculation || ''];
    if (!sel || !target || !Array.from(sel.options).some(o => o.value === target)) return;
    sel.value = target;
    onTaskChange();
  }
  function onTaskChange() {
    const t = val('gauss-task');
    const wrap = $('gauss-task-extra');
    const td = $('gauss-td-nstates'), irc = $('gauss-irc-maxpoints'), mr = $('gauss-modredundant');
    const lbl = $('gauss-task-extra-lbl');
    [td, irc, mr].forEach(el => { if (el) el.hidden = true; });
    let show = false;
    if (t === 'td') { if (td) td.hidden = false; if (lbl) lbl.textContent = '激发态数 NStates'; show = true; }
    else if (t === 'irc') { if (irc) irc.hidden = false; if (lbl) lbl.textContent = 'IRC 最大路径点数'; show = true; }
    else if (t === 'scan') { if (mr) mr.hidden = false; if (lbl) lbl.textContent = '扫描定义 ModRedundant'; show = true; }
    if (wrap) wrap.hidden = !show;
  }

  // ── 自定义泛函/基组 + 溶剂显隐 ──
  function onFuncChange() {
    const c = $('gauss-func-custom');
    if (c) c.hidden = (val('gauss-func') !== '__custom__');
  }
  function onBasisChange() {
    const c = $('gauss-basis-custom');
    if (c) c.hidden = (val('gauss-basis') !== '__custom__');
  }
  function onSolvModelChange() {
    const row = $('gauss-solv-row');
    if (row) row.hidden = !val('gauss-solv-model');
    onSolvNameChange();
  }
  function onSolvNameChange() {
    const eps = $('gauss-solv-eps-row');
    if (eps) eps.hidden = (val('gauss-solv-name') !== '__generic__') || !val('gauss-solv-model');
  }

  function funcValue() {
    const v = val('gauss-func');
    return v === '__custom__' ? (val('gauss-func-custom') || 'B3LYP') : v;
  }
  function basisValue() {
    const v = val('gauss-basis');
    return v === '__custom__' ? (val('gauss-basis-custom') || 'def2-SVP') : v;
  }

  // ── 参数组装 → engine_generate/engine_preview 用 ──
  function buildParams() {
    const extras = { basis: basisValue(), gaussian_task: val('gauss-task') || 'opt' };
    const t = val('gauss-task');
    if (t === 'td') { const n = parseInt(val('gauss-td-nstates'), 10); if (n > 0) extras.td_nstates = n; }
    if (t === 'irc') { const n = parseInt(val('gauss-irc-maxpoints'), 10); if (n > 0) extras.irc_maxpoints = n; }
    if (t === 'scan') {
      const mr = ($('gauss-modredundant') ? $('gauss-modredundant').value : '')
        .split('\n').map(s => s.trim()).filter(Boolean);
      if (mr.length) extras.modredundant = mr;
    }
    const nproc = parseInt(val('gauss-nproc'), 10); if (nproc > 0) extras.nproc = nproc;
    const mem = parseInt(val('gauss-mem'), 10); if (mem > 0) extras.mem_gb = mem;
    const chk = val('gauss-chk'); if (chk) extras.chk = chk;
    const fname = val('gauss-fname'); if (fname) extras.system = fname;
    // 溶剂
    const model = val('gauss-solv-model');
    if (model) {
      const name = val('gauss-solv-name') || 'water';
      if (name === '__generic__') {
        const eps = parseFloat(val('gauss-solv-eps'));
        const solv = { model: model };
        if (!isNaN(eps)) solv.eps = eps;
        const einf = parseFloat(val('gauss-solv-epsinf'));
        if (!isNaN(einf)) solv.epsinf = einf;
        extras.solvent = solv;
      } else {
        extras.solvent = { model: model, name: name };
      }
    }
    if (State.mixed && State.mixed.per_element &&
      Object.keys(State.mixed.per_element).length) extras.mixed_basis = State.mixed;
    const mult = parseInt(val('gauss-mult'), 10) || 1;
    const params = {
      periodic: false, functional: funcValue(),
      dispersion: val('gauss-disp') || null,
      charge: parseInt(val('gauss-charge'), 10) || 0,
      multiplicity: mult, spin: mult > 1, extras: extras,
    };
    // 结构来源:优先携带的分子,否则结构文件路径
    if (State.molStruct) params.structure = poscarText(State.molStruct, fname || 'molecule');
    else params.poscar = val('gauss-poscar');
    return params;
  }
  function hasStructure() {
    return !!State.molStruct || !!val('gauss-poscar');
  }

  // ── 预览 / 生成 / 复制 / 导出 / 提交 ──
  async function preview() {
    if (!hasStructure()) { VCS.log('Gaussian:请先带入分子或选结构文件', 'failc'); return; }
    const pre = $('gauss-preview'), head = $('gauss-prevhead');
    if (pre) { pre.style.color = ''; pre.textContent = '正在生成预览…'; }
    const r = await VCS.call('engine_preview', 'gaussian', buildParams());
    if (!r || r.ok === false || r.error) {
      if (pre) { pre.style.color = 'var(--fail)'; pre.textContent = '预览失败:' + ((r && r.error) || '未知错误'); }
      return;
    }
    State.previewText = r.text || '';
    if (pre) { pre.style.color = ''; pre.textContent = State.previewText || '(空)'; }
    if (head) head.textContent = '字符数:' + (r.chars || 0) +
      ((r.issues && r.issues.length) ? ' · 自洽提示 ' + r.issues.length + ' 条(见日志)' : '');
    (r.issues || []).forEach(i => VCS.log('Gaussian 自洽校验:' + i, 'warnc'));
    (r.warnings || []).forEach(w => VCS.log('Gaussian:' + w, 'warnc'));
  }
  async function generate() {
    if (!hasStructure()) { VCS.log('Gaussian:请先带入分子或选结构文件', 'failc'); return; }
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log('选择输出目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;
    await doGenerate(dr.path);
  }
  async function exportFile() { return generate(); }   // 导出到文件:pick_dir + 文件名(fname 决定 .gjf 名)
  async function doGenerate(outDir) {
    const btn = $('gauss-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log('生成 Gaussian 输入文件…');
    try {
      const r = await VCS.call('engine_generate', 'gaussian', buildParams(), outDir);
      if (!r || r.ok === false || r.error) {
        VCS.log('Gaussian 生成失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
      }
      (r.files || []).forEach(f => VCS.log('已生成:' + f, 'okc'));
      (r.issues || []).forEach(i => VCS.log('自洽校验:' + i, 'warnc'));
      (r.warnings || []).forEach(w => VCS.log(w, 'warnc'));
      if (!r.registered) {
        VCS.log('Gaussian 输入已生成，但 job.yaml/台账登记失败；已打开目录，不能直接提交', 'failc');
        VCS.call('open_dir', outDir);
        VCS.toast('输入已生成，但尚未纳管', 'fail');
        return;
      }
      VCS.log('Gaussian 输入已生成并加入任务列表', 'okc');
      VCS.toast('已生成并纳管 Gaussian 作业');
      if (VCS.nextStep) VCS.nextStep({
        title: 'Gaussian 作业已就绪', message: '输入文件与 job.yaml 已生成。',
        detail: '下一步：前往任务页选择服务器并提交；完成后可解析能量和生成报告。',
        primaryLabel: '前往任务页提交', page: 'jobs', focusJobDir: outDir,
      });
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  async function copyPreview() {
    if (!State.previewText) { await preview(); }
    if (!State.previewText) { VCS.toast('无预览内容可复制', 'fail'); return; }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(State.previewText);
      else legacyCopy(State.previewText);
    } catch (e) { legacyCopy(State.previewText); }
    VCS.toast('已复制到剪贴板');
  }
  function legacyCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) { /* 忽略 */ }
    ta.remove();
  }
  function submitToServer() {
    const nav = document.querySelector('nav a[data-page="jobs"]');
    if (nav) nav.click();
    VCS.toast('先生成 .gjf,再在③页「快速批量提交」上传投递');
    VCS.log('提交:请在③提交计算页用「快速批量提交」把 .gjf 建作业并投递到服务器', 'okc');
  }

  // ── 混合基组周期表弹窗 ──
  function ptPos(z) {
    const P = {
      1: [1, 1], 2: [1, 18], 3: [2, 1], 4: [2, 2], 5: [2, 13], 6: [2, 14], 7: [2, 15],
      8: [2, 16], 9: [2, 17], 10: [2, 18], 11: [3, 1], 12: [3, 2], 13: [3, 13], 14: [3, 14],
      15: [3, 15], 16: [3, 16], 17: [3, 17], 18: [3, 18], 55: [6, 1], 56: [6, 2], 57: [6, 3],
      72: [6, 4], 73: [6, 5], 74: [6, 6], 75: [6, 7], 76: [6, 8], 77: [6, 9], 78: [6, 10],
      79: [6, 11], 80: [6, 12], 81: [6, 13], 82: [6, 14], 83: [6, 15], 84: [6, 16], 85: [6, 17],
      86: [6, 18],
    };
    if (P[z]) return P[z];
    if (z >= 19 && z <= 36) return [4, z - 18];
    if (z >= 37 && z <= 54) return [5, z - 36];
    if (z >= 58 && z <= 71) return [8, 4 + (z - 58)];   // 镧系单独一行
    return [7, 1];
  }
  async function openMixed() {
    const r = await VCS.call('gauss_periodic_table');
    if (!r || r.ok === false || !r.table) { VCS.log('周期表加载失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    const elements = r.table.elements || [];
    // 已选混合基组的工作副本
    const work = State.mixed ? JSON.parse(JSON.stringify(State.mixed))
      : { default: basisValue(), per_element: {}, ecp_elements: [] };
    let cur = null;   // 当前高亮元素

    const box = document.createElement('div');
    box.className = 'pt-modal';
    box.innerHTML =
      '<div class="pt-note sub">' + VCS.esc(r.table.note || '') + '</div>' +
      '<div class="pt-grid" id="pt-grid"></div>' +
      '<div class="pt-ctrl">' +
      '<span>元素基组 <select id="pt-basis" class="ipt" style="width:auto;display:inline-block">' +
      '<option>LANL2DZ</option><option>SDD</option><option>def2-TZVP</option><option>6-31G(d)</option>' +
      '</select></span>' +
      '<label style="margin-left:10px"><input type="checkbox" id="pt-ecp"> 该元素用 ECP 赝势</label>' +
      '<button class="btn" id="pt-add">加入混合基组</button>' +
      '<button class="btn quiet" id="pt-clear">清空</button></div>' +
      '<div class="pt-selhint sub" id="pt-selhint">点周期表选一个元素,再「加入混合基组」</div>' +
      '<div class="pt-list" id="pt-list"></div>';
    const grid = box.querySelector('#pt-grid');
    elements.forEach(e => {
      const [row, col] = ptPos(e.z);
      const cell = document.createElement('span');
      cell.className = 'pt-cell cat-' + (e.category || 'main_group');
      cell.style.gridRow = row; cell.style.gridColumn = col;
      cell.dataset.sym = e.symbol;
      cell.dataset.suggest = (e.basis_suggestion || []).join(',');
      cell.textContent = e.symbol;
      cell.title = e.symbol + '(建议 ' + (e.basis_suggestion || []).join('/') + ')';
      grid.appendChild(cell);
    });

    function renderList() {
      const list = box.querySelector('#pt-list');
      const keys = Object.keys(work.per_element);
      if (!keys.length) { list.innerHTML = '<span class="sub">尚未加入任何元素;不加 = 全用单一基组</span>'; return; }
      list.innerHTML = keys.map(k => {
        const ecp = work.ecp_elements.indexOf(k) >= 0 ? ' <i class="pt-ecp-tag">ECP</i>' : '';
        return `<span class="pt-chip" data-el="${VCS.esc(k)}">${VCS.esc(k)}:${VCS.esc(work.per_element[k])}${ecp}` +
          ` <b data-del="${VCS.esc(k)}">×</b></span>`;
      }).join('');
    }
    function markGrid() {
      grid.querySelectorAll('.pt-cell').forEach(c => {
        c.classList.toggle('added', c.dataset.sym in work.per_element);
        c.classList.toggle('sel', c.dataset.sym === cur);
      });
    }
    grid.addEventListener('click', e => {
      const c = e.target.closest('.pt-cell');
      if (!c) return;
      cur = c.dataset.sym;
      const sug = (c.dataset.suggest || '').split(',').filter(Boolean);
      const bsel = box.querySelector('#pt-basis');
      if (sug.length && bsel) { for (const o of bsel.options) if (o.value === sug[0]) bsel.value = sug[0]; }
      const ecpBox = box.querySelector('#pt-ecp');
      if (ecpBox) ecpBox.checked = work.ecp_elements.indexOf(cur) >= 0;
      box.querySelector('#pt-selhint').textContent = '已选 ' + cur + ';设基组后「加入混合基组」';
      markGrid();
    });
    box.querySelector('#pt-add').addEventListener('click', () => {
      if (!cur) { VCS.toast('请先在周期表点选一个元素', 'fail'); return; }
      work.per_element[cur] = box.querySelector('#pt-basis').value;
      const ecpOn = box.querySelector('#pt-ecp').checked;
      const idx = work.ecp_elements.indexOf(cur);
      if (ecpOn && idx < 0) work.ecp_elements.push(cur);
      if (!ecpOn && idx >= 0) work.ecp_elements.splice(idx, 1);
      renderList(); markGrid();
    });
    box.querySelector('#pt-clear').addEventListener('click', () => {
      work.per_element = {}; work.ecp_elements = []; cur = null; renderList(); markGrid();
    });
    box.querySelector('#pt-list').addEventListener('click', e => {
      const d = e.target.closest('[data-del]');
      if (!d) return;
      const k = d.dataset.del;
      delete work.per_element[k];
      const i = work.ecp_elements.indexOf(k); if (i >= 0) work.ecp_elements.splice(i, 1);
      renderList(); markGrid();
    });
    renderList(); markGrid();

    const m = VCS.modal({
      title: '混合基组:元素周期表选基组(重元素赝势 / 分区精度)', body: box,
      actions: [
        { label: '关闭', quiet: true, onClick: h => h.close() },
        { label: '应用混合基组', primary: true, onClick: h => {
            work.default = basisValue();
            State.mixed = Object.keys(work.per_element).length ? work : null;
            updateMixedSummary();
            h.close();
          } },
      ],
    });
    m.el.classList.add('modal-wide');
  }
  function updateMixedSummary() {
    const el = $('gauss-mixed-summary');
    if (!el) return;
    if (!State.mixed || !Object.keys(State.mixed.per_element).length) {
      el.textContent = '未启用混合基组(用上方单一基组)';
      el.classList.remove('on');
    } else {
      const parts = Object.keys(State.mixed.per_element).map(
        k => k + ':' + State.mixed.per_element[k]);
      const ecp = State.mixed.ecp_elements.length ? ' · ECP ' + State.mixed.ecp_elements.join('/') : '';
      el.textContent = '混合基组 ' + parts.join('、') + ecp;
      el.classList.add('on');
    }
  }

  // ── 携带分子(从①分子建模) ──
  function useMolecule(struct) {
    State.molStruct = struct;
    const info = $('gauss-structinfo');
    if (info) info.textContent = '当前分子:' + (struct.formula || '') + '(' +
      (struct.natoms != null ? struct.natoms : struct.elements.length) + ' 原子)';
    onShow();
  }
  function onShow() {
    // 展开面板时确保任务表已载入
    const sel = $('gauss-task');
    if (sel && !sel.options.length) loadTasks();
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    loadTasks();
    const t = $('gauss-task'); if (t) t.addEventListener('change', onTaskChange);
    const f = $('gauss-func'); if (f) f.addEventListener('change', onFuncChange);
    const b = $('gauss-basis'); if (b) b.addEventListener('change', onBasisChange);
    const sm = $('gauss-solv-model'); if (sm) sm.addEventListener('change', onSolvModelChange);
    const sn = $('gauss-solv-name'); if (sn) sn.addEventListener('change', onSolvNameChange);
    wire('gauss-poscar-btn', async () => {
      const r = await VCS.call('pick_file', 'poscar');
      if (r && r.path) {
        const el = $('gauss-poscar'); if (el) el.value = r.path;
        State.molStruct = null;
        const info = $('gauss-structinfo'); if (info) info.textContent = '结构文件:' + r.path;
      }
    });
    wire('gauss-mixed-btn', openMixed);
    wire('gauss-preview-btn', preview);
    wire('gauss-gen-btn', generate);
    wire('gauss-copy-btn', copyPreview);
    wire('gauss-export-btn', exportFile);
    wire('gauss-submit-btn', submitToServer);
    updateMixedSummary();
  }

  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'generate') init();
  });
  document.addEventListener('vcs:calculation', syncCalculationTask);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.GaussMol = { init, onShow, useMolecule };
})();
