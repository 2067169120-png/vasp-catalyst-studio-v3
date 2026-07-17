// editor.js — 结构建模页(①):结构查看/编辑器 + 固定底层/真空检查 + 分子库浏览。
// 编辑全在 JS 状态对象(elements/coords/lattice/fixed)上进行;api 只做 解析/写出/固定层
// 纯转换(struct_load/struct_save/struct_fix_layers/struct_vacuum)。依赖 app.js 的 VCS.*
// 与 vendor/3Dmol-min.js。全部插值走 VCS.esc;零 emoji;中文文案。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const COMMON_ELEMENTS = ['H', 'B', 'C', 'N', 'O', 'F', 'Na', 'Mg', 'Al', 'Si', 'P', 'S',
    'Cl', 'K', 'Ca', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn', 'Mo', 'Ru',
    'Rh', 'Pd', 'Ag', 'W', 'Ir', 'Pt', 'Au', 'Li'];

  const State = {
    struct: null,   // {elements:[...], coords:[[x,y,z]...], lattice:[[...]], fixed:[bool], formula}
    path: null,     // 最近加载/保存路径(送去生成用)
    sel: null,      // 选中原子索引(state 序)
    undo: [],       // 撤销栈(结构快照,≥20)
  };
  let viewer = null;

  function clone(st) {
    return {
      elements: st.elements.slice(),
      coords: st.coords.map(c => c.slice()),
      lattice: st.lattice.map(r => r.slice()),
      fixed: (st.fixed || []).slice(),
      formula: st.formula,
    };
  }

  function pushUndo() {
    if (!State.struct) return;
    State.undo.push(clone(State.struct));
    if (State.undo.length > 25) State.undo.shift();
  }

  // 状态 → XYZ 文本(供 3Dmol 渲染;原子序与 state 索引一一对应)
  function stateXYZ(st) {
    const lines = [String(st.elements.length), 'vcstudio editor'];
    for (let i = 0; i < st.elements.length; i++) {
      const c = st.coords[i];
      lines.push(`${st.elements[i]} ${(+c[0]).toFixed(6)} ${(+c[1]).toFixed(6)} ${(+c[2]).toFixed(6)}`);
    }
    return lines.join('\n');
  }

  function render3D() {
    const canvas = $('ed-canvas');
    const empty = $('ed-empty');
    if (!canvas || !State.struct) return;
    if (empty) empty.hidden = true;
    if (typeof window.$3Dmol === 'undefined') {
      if (empty) { empty.hidden = false; empty.textContent = '3D 库未加载(3Dmol 缺失),编辑仍可用'; }
      return;
    }
    try {
      if (!viewer) viewer = window.$3Dmol.createViewer(canvas, { backgroundColor: '#FFFFFF' });
      viewer.clear();
      viewer.addModel(stateXYZ(State.struct), 'xyz');
      viewer.setStyle({}, { sphere: { scale: 0.3 }, stick: { radius: 0.15 } });
      // 冻结原子灰白描边;选中原子高亮橙
      (State.struct.fixed || []).forEach((f, i) => {
        if (f) viewer.setStyle({ serial: i }, { sphere: { scale: 0.3, color: '#8FA0B2' }, stick: { radius: 0.15 } });
      });
      if (State.sel != null) {
        viewer.setStyle({ serial: State.sel },
          { sphere: { scale: 0.42, color: '#E6A817' }, stick: { radius: 0.16 } });
      }
      viewer.setClickable({}, true, function (atom) {
        const idx = (atom.index != null) ? atom.index : atom.serial;
        selectAtom(idx);
      });
      viewer.zoomTo();
      viewer.render();
      setTimeout(() => { try { viewer.resize(); viewer.render(); } catch (e) { /* 忽略 */ } }, 30);
    } catch (e) {
      if (empty) { empty.hidden = false; empty.textContent = '3D 渲染失败(环境可能不支持 WebGL),编辑仍可用'; }
    }
  }

  function selectAtom(i) {
    State.sel = i;
    const st = State.struct;
    const info = $('ed-selinfo');
    if (st && i != null && i < st.elements.length) {
      const c = st.coords[i];
      const fx = (st.fixed || [])[i] ? ' · 已冻结' : '';
      if (info) info.textContent = `#${i} ${st.elements[i]}  (${(+c[0]).toFixed(3)}, ` +
        `${(+c[1]).toFixed(3)}, ${(+c[2]).toFixed(3)}) Å${fx}`;
      if ($('ed-edit')) $('ed-edit').hidden = false;
      const sel = $('ed-elem');
      if (sel) sel.value = st.elements[i];
    } else {
      if (info) info.textContent = '未选中原子';
      if ($('ed-edit')) $('ed-edit').hidden = true;
    }
    render3D();
  }

  function setStruct(struct, vacuum, path) {
    State.struct = {
      elements: struct.elements.slice(),
      coords: struct.coords.map(c => c.slice()),
      lattice: struct.lattice.map(r => r.slice()),
      fixed: new Array(struct.elements.length).fill(false),
      formula: struct.formula,
    };
    State.sel = null;
    State.undo = [];
    State.path = path || State.path;
    updateVacuum(vacuum);
    updateFixState();
    selectAtom(null);
    render3D();
  }

  function updateVacuum(v) {
    const el = $('ed-vacuum');
    if (el) el.textContent = '真空:' + (v == null ? '—' : v + ' Å');
  }
  function updateFixState() {
    const el = $('ed-fixstate');
    if (!el) return;
    const n = (State.struct && State.struct.fixed || []).filter(Boolean).length;
    el.textContent = n ? '已冻结 ' + n + ' 个原子' : '';
  }

  // ── 加载:pick_file → struct_load → 状态 ──
  async function load() {
    const r = await VCS.call('pick_file', 'poscar');
    if (r && r.error) { VCS.log('选择结构失败:' + r.error, 'failc'); return; }
    if (!r || !r.path) return;
    const out = await VCS.call('struct_load', r.path);
    if (!out || out.ok === false || out.error) {
      VCS.log('加载结构失败:' + ((out && out.error) || '未知错误'), 'failc'); return;
    }
    setStruct(out.struct, out.vacuum, r.path);
    VCS.log('已加载结构:' + r.path + '(' + out.struct.formula + ',真空 ' +
      (out.vacuum == null ? '—' : out.vacuum + ' Å') + ')', 'okc');
  }

  // ── 编辑:移动 / 删除 / 改元素(纯状态操作 + undo) ──
  function move(axis, dir) {
    if (State.struct == null || State.sel == null) return;
    const step = parseFloat(($('ed-step') && $('ed-step').value) || '0.1') || 0.1;
    pushUndo();
    State.struct.coords[State.sel][axis] += dir * step;
    selectAtom(State.sel);
    refreshVacuum();
  }
  function delAtom() {
    if (State.struct == null || State.sel == null) return;
    pushUndo();
    const i = State.sel;
    State.struct.elements.splice(i, 1);
    State.struct.coords.splice(i, 1);
    if (State.struct.fixed) State.struct.fixed.splice(i, 1);
    State.sel = null;
    selectAtom(null);
    refreshVacuum();
    VCS.log('已删除原子 #' + i, 'okc');
  }
  function changeElem() {
    if (State.struct == null || State.sel == null) return;
    const v = $('ed-elem') ? $('ed-elem').value : '';
    if (!v) return;
    pushUndo();
    State.struct.elements[State.sel] = v;
    selectAtom(State.sel);
  }
  function undo() {
    if (!State.undo.length) { VCS.log('无可撤销步骤', 'warnc'); return; }
    State.struct = State.undo.pop();
    State.sel = null;
    updateFixState();
    selectAtom(null);
    refreshVacuum();
  }

  // ── 固定底 N 层 / 真空 ──
  async function fixLayers() {
    if (!State.struct) { VCS.log('请先加载结构', 'failc'); return; }
    const n = parseInt(($('ed-fixn') && $('ed-fixn').value) || '1', 10) || 1;
    const r = await VCS.call('struct_fix_layers', stateForApi(), n);
    if (!r || r.ok === false || r.error) {
      VCS.log('固定底层失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    pushUndo();
    State.struct.fixed = r.fixed || [];
    updateVacuum(r.vacuum);
    updateFixState();
    render3D();
    VCS.log('已固定底 ' + n + ' 层(冻结 ' + r.fixed_count + ' 个原子)', 'okc');
  }
  async function refreshVacuum() {
    if (!State.struct) return;
    const r = await VCS.call('struct_vacuum', stateForApi());
    if (r && r.ok) updateVacuum(r.vacuum);
  }
  function stateForApi() {
    return {
      elements: State.struct.elements, coords: State.struct.coords,
      lattice: State.struct.lattice, fixed: State.struct.fixed,
    };
  }

  // ── 导出:保存 POSCAR / 送去生成输入 ──
  async function save() {
    if (!State.struct) { VCS.log('请先加载结构', 'failc'); return; }
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;
    const d = String(dr.path).replace(/[\\/]+$/, '');
    const sep = d.indexOf('\\') >= 0 ? '\\' : '/';
    const dest = d + sep + 'POSCAR';
    const r = await VCS.call('struct_save', stateForApi(), dest);
    if (!r || r.ok === false || r.error) {
      VCS.log('保存 POSCAR 失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    State.path = r.path;
    VCS.log('已保存 POSCAR:' + r.path, 'okc');
    VCS.call('open_dir', r.path);
    VCS.toast('已保存 POSCAR');
  }
  function sendToGenerate() {
    if (!State.path) { VCS.log('请先加载或保存结构后再送去生成', 'failc'); return; }
    const gp = $('gen-poscar');
    if (gp) { gp.value = State.path; gp.dispatchEvent(new Event('change')); }
    const nav = document.querySelector('nav a[data-page="generate"]');
    if (nav) nav.click();
    VCS.toast('已送去 ② 生成输入');
  }

  // ── 分子库浏览 ──
  async function loadMolecules() {
    const box = $('mol-grid');
    if (!box) return;
    const r = await VCS.call('molecule_list');
    const mols = (r && r.molecules) || [];
    if (!mols.length) { box.innerHTML = '<span class="sub">分子库为空</span>'; return; }
    box.innerHTML = mols.map(m =>
      `<div class="mol-card"><b>${VCS.esc(m.name)}</b>` +
      `<span>${VCS.esc(m.formula || m.name)}</span>` +
      (m.spin_hint ? `<span class="spin-hint">自旋 ${VCS.esc(String(m.spin_hint))}</span>` : '') +
      `</div>`).join('');
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    const sel = $('ed-elem');
    if (sel) sel.innerHTML = COMMON_ELEMENTS.map(e => `<option value="${e}">${e}</option>`).join('');
    wire('ed-load', load);
    wire('ed-undo', undo);
    wire('ed-del', delAtom);
    wire('ed-save', save);
    wire('ed-send', sendToGenerate);
    wire('ed-fixbtn', fixLayers);
    if (sel) sel.addEventListener('change', changeElem);
    document.querySelectorAll('#ed-edit [data-mv]').forEach(b => b.addEventListener('click', () => {
      const spec = b.dataset.mv;       // 形如 '0-'(x-)/'2+'(z+)
      move(parseInt(spec[0], 10), spec[1] === '-' ? -1 : 1);
    }));
    loadMolecules();
  }

  // 首次进入结构页时初始化(3Dmol 容器需可见才能正确测量)
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'structure') { init(); setTimeout(render3D, 40); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Editor = { reload: loadMolecules };
})();
