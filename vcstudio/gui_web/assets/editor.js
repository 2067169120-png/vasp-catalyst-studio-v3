// editor.js — 结构建模页(①):结构查看/编辑器 + 固定底层/真空检查 + 分子库浏览。
// 编辑全在 JS 状态对象(elements/coords/lattice/fixed)上进行;api 只做 解析/写出/固定层
// 纯转换(struct_load/struct_save/struct_fix_layers/struct_vacuum)。依赖 app.js 的 VCS.*
// 与 vendor/3Dmol-min.js。全部插值走 VCS.esc;零 emoji;中文文案。
// 增强(对齐 starpivot-DFT 编辑器):样式切换 / 标准视角 / 底色·标签·重置 / 保存图片 /
// 测量(键长·键角)/ 多选(全选·清除·删除选中)/ 原子列表联动。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const i18n = (key, fallback, params) => typeof VCS.t === 'function'
    ? VCS.t(key, params || {}, fallback) : fallback;
  const COMMON_ELEMENTS = ['H', 'B', 'C', 'N', 'O', 'F', 'Na', 'Mg', 'Al', 'Si', 'P', 'S',
    'Cl', 'K', 'Ca', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn', 'Mo', 'Ru',
    'Rh', 'Pd', 'Ag', 'W', 'Ir', 'Pt', 'Au', 'Li'];

  // 标准视角四元数(仅替换 getView 的旋转分量,保留平移/缩放);近似即可
  const VIEW_Q = {
    'z+': [0, 0, 0, 1], 'z-': [0, 1, 0, 0],
    'x+': [0, -0.7071, 0, 0.7071], 'x-': [0, 0.7071, 0, 0.7071],
    'y+': [0.7071, 0, 0, 0.7071], 'y-': [-0.7071, 0, 0, 0.7071],
    'iso': [-0.36, 0.36, 0.16, 0.84],
  };

  const State = {
    struct: null,   // {elements:[...], coords:[[x,y,z]...], lattice:[[...]], fixed:[bool], formula}
    path: null,     // 最近加载/保存路径(送去生成用)
    sel: null,      // 主选中原子(state 序;编辑面板用)
    selected: new Set(),  // 多选集合(全选/删除选中/原子列表联动)
    undo: [],       // 撤销栈(结构快照,≥20)
    style: 'ballstick',
    bgDark: false,  // 底色:false=白,true=主题深色
    labels: false,  // 元素序号标签
    measure: { on: false, picks: [] },
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

  // 当前显示样式 → 3Dmol setStyle spec
  function styleSpec() {
    switch (State.style) {
      case 'stick': return { stick: { radius: 0.15 } };
      case 'line': return { line: {} };
      case 'sphere': return { sphere: {} };   // 空间填充(vdW 半径)
      default: return { sphere: { scale: 0.3 }, stick: { radius: 0.15 } };  // 球棍
    }
  }
  function styleFrozen() {
    const s = styleSpec();
    if (s.sphere) s.sphere = Object.assign({}, s.sphere, { color: '#8FA0B2' });
    if (s.stick) s.stick = Object.assign({}, s.stick, { color: '#8FA0B2' });
    if (s.line) s.line = { color: '#8FA0B2' };
    return s;
  }
  function styleSel(primary) {
    return { sphere: { scale: primary ? 0.46 : 0.4, color: primary ? '#E6A817' : '#F0C24A' },
      stick: { radius: 0.16 } };
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
      const bg = State.bgDark ? '#12202E' : '#FFFFFF';
      if (!viewer) viewer = window.$3Dmol.createViewer(canvas, { backgroundColor: bg });
      viewer.setBackgroundColor(bg);
      viewer.clear();
      viewer.removeAllLabels();
      viewer.addModel(stateXYZ(State.struct), 'xyz');
      viewer.setStyle({}, styleSpec());
      // 冻结原子灰白
      (State.struct.fixed || []).forEach((f, i) => {
        if (f) viewer.setStyle({ serial: i }, styleFrozen());
      });
      // 多选高亮(橙),主选中更亮
      State.selected.forEach(i => viewer.setStyle({ serial: i }, styleSel(false)));
      if (State.sel != null) viewer.setStyle({ serial: State.sel }, styleSel(true));
      // 测量选点高亮(青)
      State.measure.picks.forEach(i => viewer.setStyle({ serial: i },
        { sphere: { scale: 0.45, color: '#28C7E0' }, stick: { radius: 0.16 } }));
      if (State.hideH) viewer.setStyle({ elem: 'H' }, {});   // 切换氢:隐藏 H(不改索引)
      if (State.labels) {
        for (let i = 0; i < State.struct.elements.length; i++) {
          const c = State.struct.coords[i];
          viewer.addLabel(`${State.struct.elements[i]}${i}`, {
            position: { x: +c[0], y: +c[1], z: +c[2] }, fontSize: 11,
            fontColor: State.bgDark ? '#DCE6F0' : '#1E2935', backgroundOpacity: 0.35,
            backgroundColor: State.bgDark ? '#0A0F17' : '#FFFFFF',
          });
        }
      }
      viewer.setClickable({}, true, function (atom) {
        const idx = (atom.index != null) ? atom.index : atom.serial;
        onAtomClick(idx);
      });
      viewer.zoomTo();
      viewer.render();
      setTimeout(() => { try { viewer.resize(); viewer.render(); } catch (e) { /* 忽略 */ } }, 30);
    } catch (e) {
      if (empty) { empty.hidden = false; empty.textContent = '3D 渲染失败(环境可能不支持 WebGL),编辑仍可用'; }
    }
  }

  // ── 原子点击分派:测量模式 vs 选中 ──
  function onAtomClick(i) {
    if (State.measure.on) { measureClick(i); return; }
    State.selected = new Set([i]);
    selectAtom(i);
  }

  function selectAtom(i) {
    State.sel = i;
    const st = State.struct;
    const info = $('ed-selinfo');
    if (st && i != null && i < st.elements.length) {
      const c = st.coords[i];
      const fx = (st.fixed || [])[i]
        ? i18n('editor.atom.frozen_suffix', ' · 已冻结') : '';
      if (info) info.textContent = `#${i} ${st.elements[i]}  (${(+c[0]).toFixed(3)}, ` +
        `${(+c[1]).toFixed(3)}, ${(+c[2]).toFixed(3)}) Å${fx}`;
      if ($('ed-edit')) $('ed-edit').hidden = false;
      const sel = $('ed-elem');
      if (sel) sel.value = st.elements[i];
    } else {
      if (info) {
        info.textContent = State.selected.size > 1
          ? i18n('editor.selection.count', '已选 {count} 个原子', {
            count: State.selected.size,
          }) : '未选中原子';
      }
      if ($('ed-edit')) $('ed-edit').hidden = true;
    }
    renderAtomList();
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
    State.selected = new Set();
    State.measure.picks = [];
    State.undo = [];
    State.path = path || State.path;
    updateVacuum(vacuum);
    updateFixState();
    selectAtom(null);
    render3D();
  }

  function updateVacuum(v) {
    const el = $('ed-vacuum');
    if (el) el.textContent = i18n('editor.vacuum.value', '真空：{value}', {
      value: v == null ? '—' : v + ' Å',
    });
  }
  function updateFixState() {
    const el = $('ed-fixstate');
    if (!el) return;
    const n = (State.struct && State.struct.fixed || []).filter(Boolean).length;
    el.textContent = n ? i18n('editor.frozen.count', '已冻结 {count} 个原子', {
      count: n,
    }) : '';
  }

  // ── 原子列表表格(右侧折叠面板;点行 = 选中联动) ──
  function renderAtomList() {
    const body = $('ed-atomlist-body');
    if (!body) return;
    const st = State.struct;
    if (!st || !st.elements.length) { body.innerHTML = '<div class="sub">无原子</div>'; return; }
    let h = '<table class="ed-atable"><thead><tr><th>#</th><th>元素</th>' +
      '<th>X</th><th>Y</th><th>Z</th></tr></thead><tbody>';
    for (let i = 0; i < st.elements.length; i++) {
      const c = st.coords[i];
      const on = State.selected.has(i) ? ' class="sel"' : '';
      h += `<tr data-atom="${i}"${on}><td>${i}</td><td>${VCS.esc(st.elements[i])}</td>` +
        `<td>${(+c[0]).toFixed(3)}</td><td>${(+c[1]).toFixed(3)}</td><td>${(+c[2]).toFixed(3)}</td></tr>`;
    }
    body.innerHTML = h + '</tbody></table>';
  }

  // ── 加载:pick_file → struct_load → 状态 ──
  async function load() {
    const r = await VCS.call('pick_file', 'poscar');
    if (r && r.error) {
      VCS.log(i18n('editor.structure.pick_failed', '选择结构失败：{error}', {
        error: r.error,
      }), 'failc'); return;
    }
    if (!r || !r.path) return;
    const out = await VCS.call('struct_load', r.path);
    if (!out || out.ok === false || out.error) {
      VCS.log(i18n('editor.structure.load_failed', '加载结构失败：{error}', {
        error: (out && out.error) || i18n('common.unknown_error', '未知错误'),
      }), 'failc'); return;
    }
    setStruct(out.struct, out.vacuum, r.path);
    VCS.log(i18n('editor.structure.loaded',
      '已加载结构：{path}（{formula}，真空 {vacuum}）', {
        path: r.path, formula: out.struct.formula,
        vacuum: out.vacuum == null ? '—' : out.vacuum + ' Å',
      }), 'okc');
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
    const i = State.sel;
    removeAtoms([i]);
    VCS.log(i18n('editor.atom.deleted', '已删除原子 #{index}', { index: i }), 'okc');
  }
  function removeAtoms(indices) {
    if (!State.struct || !indices.length) return;
    pushUndo();
    const drop = new Set(indices);
    const keep = [];
    for (let i = 0; i < State.struct.elements.length; i++) if (!drop.has(i)) keep.push(i);
    State.struct.elements = keep.map(i => State.struct.elements[i]);
    State.struct.coords = keep.map(i => State.struct.coords[i]);
    State.struct.fixed = keep.map(i => (State.struct.fixed || [])[i] || false);
    State.sel = null;
    State.selected = new Set();
    State.measure.picks = [];
    updateFixState();
    selectAtom(null);
    refreshVacuum();
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
    State.selected = new Set();
    updateFixState();
    selectAtom(null);
    refreshVacuum();
  }

  // ── 多选:全选 / 清除 / 删除选中 ──
  function selectAll() {
    if (!State.struct) return;
    State.selected = new Set(State.struct.elements.map((_, i) => i));
    State.sel = null;
    selectAtom(null);
  }
  function clearSel() {
    State.selected = new Set();
    State.sel = null;
    selectAtom(null);
  }
  function delSelected() {
    if (!State.selected.size) { VCS.toast('未选中任何原子', 'fail'); return; }
    const n = State.selected.size;
    removeAtoms(Array.from(State.selected));
    VCS.log(i18n('editor.selection.deleted', '已删除选中的 {count} 个原子', {
      count: n,
    }), 'okc');
  }

  // ── 测量:2 原子键长 / 3 原子键角 ──
  function toggleMeasure() {
    State.measure.on = !State.measure.on;
    State.measure.picks = [];
    const btn = $('ed-measure');
    if (btn) btn.classList.toggle('on', State.measure.on);
    const bar = $('ed-measbar');
    if (bar) {
      bar.hidden = !State.measure.on;
      bar.textContent = State.measure.on
        ? '测量模式:点选 2 个原子测键长,3 个原子测键角(再点「测量模式」退出)' : '';
    }
    render3D();
  }
  function vec(i, j) {
    const a = State.struct.coords[i], b = State.struct.coords[j];
    return [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
  }
  function norm(v) { return Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]); }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function measureClick(i) {
    let p = State.measure.picks;
    if (p.length >= 4) p = [];       // 测量扩到 4 原子二面角
    p.push(i);
    State.measure.picks = p;
    const bar = $('ed-measbar');
    if (p.length === 2) {
      const d = norm(vec(p[0], p[1]));
      if (bar) bar.textContent = i18n('editor.measure.distance',
        '键长 #{first}–#{second} = {value} Å（再点 1 个原子测键角）', {
          first: p[0], second: p[1], value: d.toFixed(3),
        });
    } else if (p.length === 3) {
      const v1 = vec(p[1], p[0]), v2 = vec(p[1], p[2]);
      const cos = dot(v1, v2) / (norm(v1) * norm(v2) || 1);
      const ang = Math.acos(Math.max(-1, Math.min(1, cos))) * 180 / Math.PI;
      if (bar) bar.textContent = i18n('editor.measure.angle',
        '键角 #{first}–#{second}–#{third} = {value}°（再点 1 个原子测二面角）', {
          first: p[0], second: p[1], third: p[2], value: ang.toFixed(2),
        });
    } else if (p.length === 4) {
      const b1 = vec(p[0], p[1]), b2 = vec(p[1], p[2]), b3 = vec(p[2], p[3]);
      const n1 = cross(b1, b2), n2 = cross(b2, b3);
      const m1 = cross(n1, [b2[0] / (norm(b2) || 1), b2[1] / (norm(b2) || 1), b2[2] / (norm(b2) || 1)]);
      const dih = Math.atan2(dot(m1, n2), dot(n1, n2)) * 180 / Math.PI;
      if (bar) bar.textContent = i18n('editor.measure.dihedral',
        '二面角 #{first}–#{second}–#{third}–#{fourth} = {value}°（点原子重新测量）', {
          first: p[0], second: p[1], third: p[2], fourth: p[3], value: dih.toFixed(2),
        });
    } else if (bar) {
      bar.textContent = i18n('editor.measure.first_selected',
        '已选 #{index}；再点 1 个原子测键长', { index: i });
    }
    render3D();
  }

  // ── 视角 / 底色 / 标签 / 重置 / 保存图片 / 样式 ──
  function applyView(spec) {
    if (!viewer) return;
    try {
      const q = VIEW_Q[spec];
      if (!q) return;
      const v = viewer.getView();
      const nv = v.slice();
      nv[4] = q[0]; nv[5] = q[1]; nv[6] = q[2]; nv[7] = q[3];
      viewer.setView(nv);
      viewer.render();
    } catch (e) { /* 视图矩阵接口差异忽略 */ }
  }
  function resetView() { if (viewer) { try { viewer.zoomTo(); viewer.render(); } catch (e) { /* 忽略 */ } } }
  function toggleBg() { State.bgDark = !State.bgDark; render3D(); }
  function toggleLabels() {
    State.labels = !State.labels;
    const btn = $('ed-labels');
    if (btn) btn.classList.toggle('on', State.labels);
    render3D();
  }
  function setStyle(v) { State.style = v; render3D(); }
  function saveImage() {
    if (!viewer) { VCS.log('无 3D 视图可保存(3Dmol 未加载)', 'failc'); return; }
    try {
      const uri = viewer.pngURI();
      const a = document.createElement('a');
      a.href = uri; a.download = 'structure.png';
      document.body.appendChild(a); a.click(); a.remove();
      VCS.toast('已保存结构图片');
    } catch (e) {
      VCS.log(i18n('editor.image.save_failed', '保存图片失败：{error}', {
        error: e,
      }), 'failc');
    }
  }
  function toggleAtomList() {
    const box = $('ed-atomlist');
    if (box) { box.hidden = !box.hidden; if (!box.hidden) renderAtomList(); }
  }

  // ── 固定底 N 层 / 真空 ──
  async function fixLayers() {
    if (!State.struct) { VCS.log('请先加载结构', 'failc'); return; }
    const n = parseInt(($('ed-fixn') && $('ed-fixn').value) || '1', 10) || 1;
    const r = await VCS.call('struct_fix_layers', stateForApi(), n);
    if (!r || r.ok === false || r.error) {
      VCS.log(i18n('editor.layers.fix_failed', '固定底层失败：{error}', {
        error: (r && r.error) || i18n('common.unknown_error', '未知错误'),
      }), 'failc'); return;
    }
    pushUndo();
    State.struct.fixed = r.fixed || [];
    updateVacuum(r.vacuum);
    updateFixState();
    render3D();
    VCS.log(i18n('editor.layers.fixed', '已固定底 {layers} 层（冻结 {count} 个原子）', {
      layers: n, count: r.fixed_count,
    }), 'okc');
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
    if (dr && dr.error) {
      VCS.log(i18n('editor.directory.pick_failed', '选择目录失败：{error}', {
        error: dr.error,
      }), 'failc'); return;
    }
    if (!dr || !dr.path) return;
    const d = String(dr.path).replace(/[\\/]+$/, '');
    const sep = d.indexOf('\\') >= 0 ? '\\' : '/';
    const dest = d + sep + 'POSCAR';
    const r = await VCS.call('struct_save', stateForApi(), dest);
    if (!r || r.ok === false || r.error) {
      VCS.log(i18n('editor.poscar.save_failed', '保存 POSCAR 失败：{error}', {
        error: (r && r.error) || i18n('common.unknown_error', '未知错误'),
      }), 'failc'); return;
    }
    State.path = r.path;
    VCS.log(i18n('editor.poscar.saved', '已保存 POSCAR：{path}', { path: r.path }), 'okc');
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

  // ── 切换氢显示 ──
  function toggleHydrogens() {
    State.hideH = !State.hideH;
    const btn = $('ed-hydrogens');
    if (btn) btn.classList.toggle('on', State.hideH);
    render3D();
  }

  // ── 输入原子坐标(粘贴 xyz 文本 → 载入编辑器)──
  function pasteXyz() {
    const box = document.createElement('div');
    box.innerHTML = '<div class="sub" style="margin-bottom:6px">' + VCS.esc(i18n(
      'editor.xyz.instructions',
      '每行一个原子：元素 x y z（单位 Å）；可含/不含 XYZ 头两行。')) + '</div>' +
      '<textarea id="ed-xyz-ta" class="ipt" rows="10" spellcheck="false" ' +
      'placeholder="O   0.000   0.000   0.000&#10;H   0.757   0.586   0.000&#10;H  -0.757   0.586   0.000"></textarea>';
    const m = VCS.modal({
      title: '输入原子坐标(xyz)', body: box,
      actions: [
        { label: '取消', quiet: true, onClick: h => h.close() },
        { label: '载入', primary: true, onClick: h => { if (applyXyz(box)) h.close(); } },
      ],
    });
    const ta = box.querySelector('#ed-xyz-ta');
    if (ta) ta.focus();
  }
  function applyXyz(box) {
    const txt = (box.querySelector('#ed-xyz-ta') || {}).value || '';
    const elements = [], coords = [];
    txt.split('\n').forEach(line => {
      const p = line.trim().split(/[\s,]+/);
      if (p.length >= 4 && /^[A-Za-z]{1,2}$/.test(p[0])) {
        const x = parseFloat(p[1]), y = parseFloat(p[2]), z = parseFloat(p[3]);
        if (isFinite(x) && isFinite(y) && isFinite(z)) {
          elements.push(p[0][0].toUpperCase() + (p[0][1] || '').toLowerCase());
          coords.push([x, y, z]);
        }
      }
    });
    if (!elements.length) { VCS.log('未解析到有效原子行(元素 x y z)', 'failc'); return false; }
    const counts = {};
    elements.forEach(e => { counts[e] = (counts[e] || 0) + 1; });
    const formula = Object.keys(counts).map(e => e + (counts[e] > 1 ? counts[e] : '')).join('');
    setStruct({ elements: elements, coords: coords, fixed: elements.map(() => false), formula: formula },
      null, null);
    VCS.log(i18n('editor.xyz.loaded', '已载入粘贴坐标：{count} 原子（{formula}）', {
      count: elements.length, formula,
    }), 'okc');
    VCS.toast(i18n('editor.xyz.loaded_count', '已载入 {count} 原子', {
      count: elements.length,
    }));
    return true;
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
      (m.spin_hint ? `<span class="spin-hint">${VCS.esc(i18n(
        'editor.molecule.spin', '自旋 {spin}', { spin: String(m.spin_hint) }
      ))}</span>` : '') +
      `</div>`).join('');
  }

  // ── Structure Source Hub + 通用 slab / 几何位点向导 ──
  // 这个闭环只保存 opaque token。后端返回对象始终按白名单字段渲染，绝不把路径、
  // 原始结构文本或凭据回填到页面。外部 provider 是可选 gateway capability；本地
  // picker 与远程错误完全解耦。
  const SourceHubState = {
    providers: [],
    results: { bulk: [], ads: [] },
    previews: { bulk: null, ads: null },
    sourceTokens: { bulk: null, ads: null },
    viewers: { bulk: null, ads: null },
    dryRun: null,
    operationToken: null,
    outputSelectionToken: null,
    outputDisplayName: '',
    created: false,
  };

  const sourceRoleIds = role => ({
    results: `src-${role}-results`, preview: `src-${role}-preview`,
    meta: `src-${role}-meta`, view3d: `src-${role}-view3d`,
    top: `src-${role}-top`, provenance: `src-${role}-provenance`,
    confirm: `src-${role}-confirm`, status: `src-${role}-status`,
  });

  function publicText(value) {
    let text = String(value == null ? '' : value);
    // Defensive redaction: DTOs are required to be path-free, but UI rendering still fails closed.
    text = text.replace(/[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*/g,
      i18n('structure.hub.hidden_local_value', '[local value hidden]'));
    text = text.replace(/(^|\s)\/(?:[^/\s]+\/)+[^/\s]*/g, (match, lead) =>
      lead + i18n('structure.hub.hidden_local_value', '[local value hidden]'));
    return text;
  }

  function setSourceStatus(role, message, failed) {
    const el = $(sourceRoleIds(role).status);
    if (!el) return;
    el.textContent = publicText(message || '');
    el.classList.toggle('fail', !!failed);
  }

  function setRemoteStatus(message, failed) {
    const el = $('source-remote-status');
    if (!el) return;
    el.textContent = publicText(message || '');
    el.classList.toggle('fail', !!failed);
  }

  function setCreateStatus(message, failed) {
    const el = $('surface-create-status');
    if (!el) return;
    el.textContent = publicText(message || '');
    el.classList.toggle('fail', !!failed);
  }

  function normalizeProviders(out) {
    const raw = out && (out.providers || out.capabilities || out.provider_capabilities);
    const rows = Array.isArray(raw) ? raw : ((raw && typeof raw === 'object')
      ? Object.keys(raw).map(id => Object.assign({ id }, raw[id] || {})) : []);
    return rows.map(row => {
      const labels = row.label;
      const lang = window.VCS && VCS.i18n && VCS.i18n.lang === 'en' ? 'en' : 'zh';
      const localizedLabel = labels && typeof labels === 'object'
        ? (labels[lang] || labels.en || labels.zh || '') : labels;
      return {
      id: String(row.id || row.provider || row.key || ''),
      label: String(localizedLabel || row.name || row.id || row.provider || ''),
      labels: labels && typeof labels === 'object' ? labels : null,
      available: row.available !== false && row.enabled !== false && row.installed !== false,
      reason: String(row.reason || row.error || ''),
      remote: row.remote !== false && row.network !== false && row.kind !== 'local',
      };
    }).filter(row => row.id && row.remote);
  }

  function renderProviderOptions() {
    const select = $('source-provider');
    const search = $('source-search');
    const capability = $('source-capability-status');
    if (!select) return;
    select.textContent = '';
    SourceHubState.providers.forEach(provider => {
      const option = document.createElement('option');
      option.value = provider.id;
      const lang = window.VCS && VCS.i18n && VCS.i18n.lang === 'en' ? 'en' : 'zh';
      const label = provider.labels
        ? (provider.labels[lang] || provider.labels.en || provider.labels.zh) : provider.label;
      option.textContent = publicText(label || provider.id) +
        (provider.available ? '' : ' · ' + i18n('structure.hub.gateway.unavailable_short', 'unavailable'));
      option.disabled = !provider.available;
      select.appendChild(option);
    });
    const available = SourceHubState.providers.filter(provider => provider.available);
    if (available.length) select.value = available[0].id;
    select.disabled = !available.length;
    if (search) search.disabled = !available.length;
    if (capability) capability.textContent = available.length
      ? i18n('structure.hub.gateway.available_count', '{count} provider(s) available', {
        count: available.length,
      })
      : i18n('structure.hub.gateway.unavailable', 'External reference gateway unavailable');
  }

  async function loadSourceCapabilities() {
    const out = await VCS.call('structure_source_capabilities');
    SourceHubState.providers = normalizeProviders(out);
    renderProviderOptions();
    if (!out || out.error || !SourceHubState.providers.some(provider => provider.available)) {
      setRemoteStatus(i18n('structure.hub.gateway.local_still_available',
        'External reference gateway unavailable. Local CIF/POSCAR selection is still available.'),
      false);
    } else {
      setRemoteStatus('');
    }
    return SourceHubState.providers;
  }

  function methodSummary(row) {
    const method = row && (row.method_metadata || row.method);
    if (typeof method === 'string') return method;
    if (!method || typeof method !== 'object') return '';
    return [method.name, method.version, method.description, method.level_of_theory,
      method.format, method.parser]
      .filter(Boolean).map(publicText).join(' · ');
  }

  function metadataSummary(value, fields) {
    if (typeof value === 'string') return publicText(value);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
    return fields.filter(field => value[field] != null)
      .map(field => publicText(value[field])).filter(Boolean).join(' · ');
  }

  function normalizeSourceResult(row) {
    if (!row || typeof row !== 'object') return null;
    const token = row.token || row.result_token || row.source_token;
    if (typeof token !== 'string' || !token) return null;
    const method = row.method_metadata || row.method;
    return {
      token,
      provider: publicText(method && typeof method === 'object' ? method.provider : ''),
      sourceId: publicText(row.source_id || row.sourceId || row.database_id || row.material_id || ''),
      formula: publicText(row.formula || ''),
      license: metadataSummary(row.license, ['name', 'spdx_id', 'url']),
      citation: metadataSummary(row.citation, ['text', 'doi', 'url']),
      method: publicText(methodSummary(row)),
      structureHash: publicText(row.structure_hash || row.structureHash || row.raw_structure_hash || ''),
    };
  }

  function sourceResultHtml(row, role) {
    const pieces = [row.provider, row.sourceId, row.formula].filter(Boolean);
    const evidence = [];
    if (row.license) evidence.push(i18n('structure.hub.result.license', 'License: {value}', {
      value: row.license,
    }));
    if (row.citation) evidence.push(i18n('structure.hub.result.citation', 'Citation: {value}', {
      value: row.citation,
    }));
    if (row.method) evidence.push(i18n('structure.hub.result.method', 'Method: {value}', {
      value: row.method,
    }));
    if (row.structureHash) evidence.push(i18n('structure.hub.result.hash', 'Structure hash: {value}', {
      value: row.structureHash,
    }));
    return '<article class="source-result" role="listitem">' +
      `<div><b>${VCS.esc(pieces.join(' · ') || i18n('structure.hub.result.unnamed', 'Structure result'))}</b>` +
      evidence.map(item => `<span>${VCS.esc(item)}</span>`).join('') + '</div>' +
      `<button type="button" class="btn" data-source-preview="${VCS.esc(role)}" ` +
      `data-source-token="${VCS.esc(row.token)}">${VCS.esc(i18n(
        'structure.hub.result.preview', 'Preview'))}</button></article>`;
  }

  function showSourceResults(role, rows) {
    const normalized = (Array.isArray(rows) ? rows : []).map(normalizeSourceResult).filter(Boolean);
    SourceHubState.results[role] = normalized;
    const box = $(sourceRoleIds(role).results);
    if (!box) return normalized;
    box.innerHTML = normalized.length
      ? normalized.map(row => sourceResultHtml(row, role)).join('')
      : `<p class="source-empty">${VCS.esc(i18n('structure.hub.result.empty', 'No path-free results returned.'))}</p>`;
    return normalized;
  }

  function invalidateDryRun(message) {
    SourceHubState.dryRun = null;
    SourceHubState.operationToken = null;
    SourceHubState.outputSelectionToken = null;
    SourceHubState.outputDisplayName = '';
    SourceHubState.created = false;
    const result = $('surface-dry-result');
    if (result) result.textContent = message || '';
    const output = $('surface-output-select');
    const outputName = $('surface-output-name');
    const ack = $('surface-candidate-ack');
    const create = $('surface-create-candidates');
    if (output) output.disabled = true;
    if (outputName) outputName.textContent = '';
    if (ack) { ack.checked = false; ack.disabled = true; }
    if (create) create.disabled = true;
    setCreateStatus('');
  }

  function resetSourceConfirmation(role) {
    SourceHubState.sourceTokens[role] = null;
    const confirm = $(sourceRoleIds(role).confirm);
    if (confirm) confirm.disabled = !SourceHubState.previews[role];
    if (role === 'bulk') {
      const dry = $('surface-dry-run');
      if (dry) dry.disabled = true;
    }
    invalidateDryRun('');
  }

  function render3DPreview(role, preview) {
    const box = $(sourceRoleIds(role).view3d);
    if (!box) return;
    if (SourceHubState.viewers[role]) {
      try { SourceHubState.viewers[role].clear(); } catch (e) { /* no-op */ }
      SourceHubState.viewers[role] = null;
    }
    box.textContent = '';
    const view = preview && preview.view;
    if (!view || typeof view.xyz !== 'string' || !view.xyz.trim()) {
      box.textContent = i18n('structure.hub.preview.no_3d', 'No 3D preview data');
      return;
    }
    if (typeof window.$3Dmol === 'undefined') {
      box.textContent = i18n('structure.hub.preview.no_webgl',
        '3D renderer unavailable; metadata and top view remain usable.');
      return;
    }
    try {
      const sourceViewer = window.$3Dmol.createViewer(box, { backgroundColor: '#FFFFFF' });
      sourceViewer.addModel(view.xyz, 'xyz');
      sourceViewer.setStyle({}, { sphere: { scale: 0.3 }, stick: { radius: 0.15 } });
      sourceViewer.zoomTo();
      sourceViewer.render();
      SourceHubState.viewers[role] = sourceViewer;
      setTimeout(() => {
        try { sourceViewer.resize(); sourceViewer.render(); } catch (e) { /* no-op */ }
      }, 30);
    } catch (error) {
      box.textContent = i18n('structure.hub.preview.render_failed',
        '3D preview failed; metadata and top view remain usable.');
    }
  }

  function xyzTopPoints(xyz) {
    if (typeof xyz !== 'string') return [];
    return xyz.split(/\r?\n/).slice(2).map(line => {
      const parts = line.trim().split(/\s+/);
      return parts.length >= 4 ? { element: parts[0], x: +parts[1], y: +parts[2] } : null;
    }).filter(point => point && Number.isFinite(point.x) && Number.isFinite(point.y));
  }

  function topViewPoints(preview) {
    const top = (preview && preview.top_view) || {};
    const raw = Array.isArray(top.points) ? top.points : (Array.isArray(top.atoms) ? top.atoms : []);
    const points = raw.map(point => {
      const coords = point.coords || point.position || [];
      return {
        element: publicText(point.element || point.symbol || point.label || ''),
        x: +(point.x != null ? point.x : coords[0]),
        y: +(point.y != null ? point.y : coords[1]),
        group: publicText(point.equivalence_group || point.group || ''),
      };
    }).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
    if (points.length) return points;
    return xyzTopPoints(top.xyz || (preview && preview.view && preview.view.xyz));
  }

  function renderTopView(role, preview) {
    const box = $(sourceRoleIds(role).top);
    if (!box) return;
    box.textContent = '';
    const points = topViewPoints(preview);
    if (!points.length || typeof document.createElementNS !== 'function') {
      box.textContent = i18n('structure.hub.preview.no_top', 'No top-view coordinates');
      return;
    }
    const xs = points.map(point => point.x), ys = points.map(point => point.y);
    const minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    const minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    const dx = Math.max(maxX - minX, 1), dy = Math.max(maxY - minY, 1);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 320 220');
    svg.setAttribute('aria-hidden', 'true');
    points.forEach((point, index) => {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', String(22 + ((point.x - minX) / dx) * 276));
      circle.setAttribute('cy', String(198 - ((point.y - minY) / dy) * 176));
      circle.setAttribute('r', '7');
      circle.setAttribute('data-equivalence-group', point.group || '');
      const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      title.textContent = `${point.element || '?'} ${index}`;
      circle.appendChild(title);
      svg.appendChild(circle);
    });
    box.appendChild(svg);
  }

  function builderParametersText(parameters) {
    if (!parameters || typeof parameters !== 'object') return '';
    const keys = ['miller', 'termination', 'layers', 'vacuum', 'vacuum_angstrom',
      'fixed_layers', 'surface_sides', 'symmetry_deduplicate'];
    return keys.filter(key => parameters[key] != null)
      .map(key => `${key}=${publicText(Array.isArray(parameters[key])
        ? parameters[key].join(',') : parameters[key])}`).join('; ');
  }

  function renderProvenance(role, preview) {
    const list = $(sourceRoleIds(role).provenance);
    if (!list) return;
    list.textContent = '';
    const provenance = (preview && preview.provenance) || {};
    const builder = provenance.builder || {};
    const fields = [
      ['structure.hub.prov.provider', 'Provider', provenance.provider],
      ['structure.hub.prov.database_id', 'Database ID / query',
        provenance.database_id || provenance.source_id],
      ['structure.hub.prov.query', 'Query', provenance.query],
      ['structure.hub.prov.retrieved_at', 'Retrieved', provenance.retrieved_at],
      ['structure.hub.prov.license', 'License', metadataSummary(
        provenance.license || preview.license, ['name', 'spdx_id', 'url'])],
      ['structure.hub.prov.citation', 'Citation', metadataSummary(
        provenance.citation || preview.citation, ['text', 'doi', 'url'])],
      ['structure.hub.prov.method', 'Method metadata', methodSummary({
        method: provenance.method || preview.method,
      })],
      ['structure.hub.prov.raw_hash', 'Raw structure hash',
        provenance.raw_structure_hash || preview.raw_structure_hash || preview.structure_hash],
      ['structure.hub.prov.builder', 'Builder / version',
        [builder.name || provenance.builder_name, builder.version || provenance.builder_version]
          .filter(Boolean).join(' · ')],
      ['structure.hub.prov.parameters', 'Builder parameters',
        builderParametersText(builder.parameters || provenance.builder_parameters)],
    ];
    fields.filter(field => field[2] != null && String(field[2]).trim()).forEach(field => {
      const term = document.createElement('dt');
      const value = document.createElement('dd');
      term.textContent = i18n(field[0], field[1]);
      value.textContent = publicText(field[2]);
      list.appendChild(term); list.appendChild(value);
    });
  }

  function renderSourcePreview(role, preview) {
    const ids = sourceRoleIds(role);
    const panel = $(ids.preview);
    const meta = $(ids.meta);
    if (panel) panel.hidden = false;
    if (meta) meta.textContent = i18n('structure.hub.preview.summary',
      '{formula} · {count} atoms · hash {hash}', {
        formula: publicText(preview.formula || '—'),
        count: preview.natoms == null ? '—' : preview.natoms,
        hash: publicText(preview.structure_hash || preview.raw_structure_hash || '—'),
      });
    render3DPreview(role, preview);
    renderTopView(role, preview);
    renderProvenance(role, preview);
    const confirm = $(ids.confirm);
    if (confirm) confirm.disabled = false;
  }

  async function previewSource(role, token) {
    if (role !== 'bulk' && role !== 'ads') return false;
    setSourceStatus(role, i18n('structure.hub.preview.loading', 'Loading source preview…'), false);
    SourceHubState.previews[role] = null;
    resetSourceConfirmation(role);
    const out = await VCS.call('structure_source_preview', token);
    const preview = out && out.preview;
    if (!out || out.error || out.ok === false || !preview || typeof preview !== 'object') {
      setSourceStatus(role, i18n('structure.hub.preview.failed', 'Preview failed: {error}', {
        error: (out && out.error) || i18n('common.unknown_error', 'Unknown error'),
      }), true);
      return false;
    }
    SourceHubState.previews[role] = Object.assign({}, preview, {
      token: preview.token || token,
    });
    renderSourcePreview(role, SourceHubState.previews[role]);
    setSourceStatus(role, i18n('structure.hub.preview.ready',
      'Preview ready. Confirm this source before using it.'), false);
    const confirm = $(sourceRoleIds(role).confirm);
    if (confirm) confirm.focus();
    return true;
  }

  async function confirmSource(role) {
    const preview = SourceHubState.previews[role];
    if (!preview || typeof preview.token !== 'string') return false;
    const button = $(sourceRoleIds(role).confirm);
    if (button) button.disabled = true;
    const out = await VCS.call('structure_source_confirm', preview.token);
    if (!out || out.error || out.ok === false || out.confirmed !== true ||
        typeof out.source_token !== 'string' || !out.source_token) {
      if (button) button.disabled = false;
      setSourceStatus(role, i18n('structure.hub.confirm.failed',
        'Source confirmation failed: {error}', {
          error: (out && out.error) || i18n('structure.hub.confirm.invalid',
            'server did not return a confirmed opaque token'),
        }), true);
      return false;
    }
    SourceHubState.sourceTokens[role] = out.source_token;
    invalidateDryRun('');
    if (role === 'bulk') {
      const dry = $('surface-dry-run');
      if (dry) dry.disabled = false;
    }
    setSourceStatus(role, role === 'bulk'
      ? i18n('structure.hub.bulk.confirmed', 'Bulk/substrate source confirmed.')
      : i18n('structure.hub.ads.confirmed', 'Adsorbate source confirmed.'), false);
    return true;
  }

  async function selectLocalSource(role) {
    setSourceStatus(role, i18n('structure.hub.local.selecting', 'Waiting for system file picker…'), false);
    const out = await VCS.call('structure_source_select_local');
    if (!out || out.cancelled) {
      setSourceStatus(role, i18n('structure.hub.local.cancelled', 'Selection cancelled.'), false);
      return false;
    }
    if (out.error || out.ok === false) {
      setSourceStatus(role, i18n('structure.hub.local.failed', 'Local selection failed: {error}', {
        error: out.error || i18n('common.unknown_error', 'Unknown error'),
      }), true);
      return false;
    }
    const rows = showSourceResults(role, out.results || []);
    setSourceStatus(role, rows.length
      ? i18n('structure.hub.result.count', '{count} result(s); preview one to continue.', {
        count: rows.length,
      }) : i18n('structure.hub.result.empty', 'No path-free results returned.'), !rows.length);
    const results = $(sourceRoleIds(role).results);
    if (results) results.focus && results.focus();
    return !!rows.length;
  }

  async function searchSourceGateway() {
    const provider = $('source-provider');
    const query = $('source-query');
    const target = $('source-target');
    const providerId = provider ? provider.value : '';
    const queryText = query ? query.value.trim() : '';
    const role = target && target.value === 'ads' ? 'ads' : 'bulk';
    if (!providerId || !queryText) {
      setRemoteStatus(i18n('structure.hub.gateway.query_required',
        'Choose an available provider and enter a query.'), true);
      return false;
    }
    setRemoteStatus(i18n('structure.hub.gateway.searching', 'Searching external reference gateway…'), false);
    const out = await VCS.call('structure_source_search', providerId, queryText);
    if (!out || out.error || out.ok === false) {
      setRemoteStatus(i18n('structure.hub.gateway.search_failed',
        'External reference gateway unavailable: {error}', {
          error: (out && out.error) || i18n('common.unknown_error', 'Unknown error'),
        }), true);
      return false;
    }
    const rows = showSourceResults(role, out.results || []);
    setRemoteStatus(rows.length
      ? i18n('structure.hub.gateway.result_count', '{count} result(s) sent to {target}.', {
        count: rows.length,
        target: role === 'bulk' ? i18n('structure.hub.gateway.target_bulk', 'bulk/substrate')
          : i18n('structure.hub.gateway.target_ads', 'adsorbate'),
      }) : i18n('structure.hub.result.empty', 'No path-free results returned.'), !rows.length);
    return !!rows.length;
  }

  function clearAdsorbateSource() {
    SourceHubState.previews.ads = null;
    SourceHubState.sourceTokens.ads = null;
    SourceHubState.results.ads = [];
    const ids = sourceRoleIds('ads');
    if ($(ids.results)) $(ids.results).textContent = '';
    if ($(ids.preview)) $(ids.preview).hidden = true;
    if ($(ids.confirm)) $(ids.confirm).disabled = true;
    if (SourceHubState.viewers.ads) {
      try { SourceHubState.viewers.ads.clear(); } catch (e) { /* no-op */ }
      SourceHubState.viewers.ads = null;
    }
    invalidateDryRun('');
    setSourceStatus('ads', i18n('structure.hub.ads.cleared',
      'Adsorbate cleared; dry-run will enumerate clean-slab geometric sites.'), false);
  }

  function finiteNumber(id, label, options) {
    const el = $(id);
    const value = el ? Number(el.value) : NaN;
    const opts = options || {};
    if (!Number.isFinite(value) || (opts.integer && !Number.isInteger(value)) ||
        (opts.min != null && value < opts.min) || (opts.max != null && value > opts.max)) {
      throw new Error(i18n('structure.hub.validation.number',
        '{label} has an invalid value.', { label }));
    }
    return value;
  }

  function buildSurfaceRequests() {
    const miller = [
      finiteNumber('surface-h', 'Miller h', { integer: true }),
      finiteNumber('surface-k', 'Miller k', { integer: true }),
      finiteNumber('surface-l', 'Miller l', { integer: true }),
    ];
    if (miller.every(value => value === 0)) {
      throw new Error(i18n('structure.hub.validation.miller_zero',
        'Miller indices cannot all be zero.'));
    }
    const selectedSites = Array.from(document.querySelectorAll(
      'input[name="surface-site-family"]:checked')).map(input => input.value);
    if (!selectedSites.length) {
      throw new Error(i18n('structure.hub.validation.site_required',
        'Select at least one geometric site family.'));
    }
    const bindingEl = $('surface-binding-atom');
    const bindingValue = bindingEl && bindingEl.value !== ''
      ? finiteNumber('surface-binding-atom', i18n('structure.hub.site.binding_atom',
        'Binding atom index'), { integer: true, min: 0 }) : null;
    if (!$('surface-dedup') || !$('surface-dedup').checked) {
      throw new Error(i18n('structure.hub.validation.dedup_required',
        'Symmetry deduplication is required for this deterministic workflow.'));
    }
    const sides = ($('surface-sides') && $('surface-sides').value) || 'top';
    const rotations = (($('surface-rotations') && $('surface-rotations').value) || '0')
      .split(',').map(value => Number(value));
    const termination = ($('surface-termination') && $('surface-termination').value.trim()) || '';
    const slabRequest = {
      miller,
      layers: finiteNumber('surface-layers', i18n('structure.hub.slab.layers', 'Layers'),
        { integer: true, min: 1 }),
      vacuum: finiteNumber('surface-vacuum',
        i18n('structure.hub.slab.vacuum', 'Vacuum'), { min: 5 }),
      fixed_layers: finiteNumber('surface-fixed',
        i18n('structure.hub.slab.fixed', 'Fixed bottom layers'), { integer: true, min: 0 }),
      surface_sides: sides,
    };
    // Empty/"auto" means enumerate server-authoritative terminations. The core intentionally
    // rejects the literal "auto", so never put that sentinel on the bridge.
    if (termination && termination.toLowerCase() !== 'auto') {
      slabRequest.termination = termination;
    }
    const siteRequest = {
      site_kinds: selectedSites,
      binding_atom: bindingValue,
      orientation: ($('surface-orientation') && $('surface-orientation').value) || 'normal',
      rotations,
      coverage: finiteNumber('surface-coverage',
        i18n('structure.hub.site.coverage', 'Coverage'), { min: 0.01, max: 1 }),
      sides,
      min_distance: finiteNumber('surface-min-distance',
        i18n('structure.hub.site.min_distance', 'Minimum allowed distance'), { min: 1 }),
    };
    if (SourceHubState.sourceTokens.ads) {
      siteRequest.adsorbate_source_token = SourceHubState.sourceTokens.ads;
    }
    return { slabRequest, siteRequest };
  }

  function dryCandidateHtml(row, index) {
    const parameters = (row.parameters && typeof row.parameters === 'object')
      ? row.parameters : {};
    const miller = row.miller || parameters.miller || parameters.miller_requested;
    const termination = row.termination && typeof row.termination === 'object'
      ? (row.termination.termination_id || row.termination.id) : row.termination;
    const layers = row.layers == null ? parameters.layers : row.layers;
    const summary = [row.formula, miller && `(${miller.join(' ')})`, termination,
      layers != null ? i18n('structure.hub.dry.layers', '{count} layers', {
        count: layers,
      }) : '', row.structure_hash].filter(Boolean).map(publicText).join(' · ');
    const sites = row.site_counts || row.sites || {};
    const siteCounts = Array.isArray(sites) ? sites.reduce((counts, site) => {
      const kind = publicText((site && site.kind) || 'other');
      counts[kind] = (counts[kind] || 0) + 1;
      return counts;
    }, {}) : sites;
    const siteText = (siteCounts && typeof siteCounts === 'object' && !Array.isArray(siteCounts))
      ? Object.keys(siteCounts).sort().map(key => `${publicText(key)}=${publicText(siteCounts[key])}`).join(', ')
      : publicText(siteCounts || '');
    const rejections = Array.isArray(row.rejections) ? row.rejections : [];
    const equivalenceCount = Array.isArray(row.equivalence_groups)
      ? row.equivalence_groups.length : row.equivalence_groups;
    const builder = row.provenance || row.builder || {};
    const builderText = [builder.builder, builder.name,
      builder.builder_version || builder.version]
      .filter(Boolean).map(publicText).join(' · ');
    const parameterText = builderParametersText(row.parameters || builder.parameters);
    return '<article class="surface-candidate">' +
      `<b>${VCS.esc(summary || i18n('structure.hub.dry.candidate', 'Candidate {index}', {
        index: index + 1,
      }))}</b>` +
      (siteText ? `<span>${VCS.esc(i18n('structure.hub.dry.site_counts',
        'Geometric sites: {value}', { value: siteText }))}</span>` : '') +
      (equivalenceCount != null ? `<span>${VCS.esc(i18n(
        'structure.hub.dry.equivalence_groups', 'Geometric symmetry-candidate groups: {count}', {
          count: equivalenceCount,
        }))}</span>` : '') +
      (row.minimum_distance != null ? `<span>${VCS.esc(i18n(
        'structure.hub.dry.minimum_distance', 'Minimum distance: {value} Å', {
          value: publicText(row.minimum_distance),
        }))}</span>` : '') +
      (builderText ? `<span>${VCS.esc(i18n('structure.hub.dry.builder',
        'Builder / version: {value}', { value: builderText }))}</span>` : '') +
      (parameterText ? `<span>${VCS.esc(i18n('structure.hub.dry.parameters',
        'Builder parameters: {value}', { value: parameterText }))}</span>` : '') +
      (rejections.length ? '<ul class="surface-rejections">' + rejections.map(rejection => {
        const text = (rejection && typeof rejection === 'object')
          ? [rejection.code, rejection.reason, rejection.count].filter(value => value != null).join(' · ')
          : rejection;
        return `<li>${VCS.esc(publicText(text))}</li>`;
      }).join('') + '</ul>' : '') + '</article>';
  }

  function renderDryRun(dryRun) {
    const box = $('surface-dry-result');
    if (!box) return;
    const slabs = Array.isArray(dryRun.slabs) ? dryRun.slabs : [];
    const warnings = Array.isArray(dryRun.warnings) ? dryRun.warnings : [];
    const mode = SourceHubState.sourceTokens.ads
      ? i18n('structure.hub.dry.mode_ads', 'Adsorption-configuration candidates')
      : i18n('structure.hub.dry.mode_clean', 'Clean-slab candidates; geometric sites only');
    box.innerHTML = `<div class="surface-dry-summary"><b>${VCS.esc(i18n(
      'structure.hub.dry.ready', 'Dry-run ready: {count} slab candidate(s)', {
        count: slabs.length,
      }))}</b><span>${VCS.esc(mode)}</span><span>${VCS.esc(i18n(
        'structure.hub.site.boundary', 'Geometric sites are not active sites.'))}</span></div>` +
      warnings.map(warning => `<div class="source-warning">${VCS.esc(publicText(warning))}</div>`).join('') +
      slabs.map(dryCandidateHtml).join('');
    box.focus();
  }

  async function runSurfaceDryRun() {
    if (!SourceHubState.sourceTokens.bulk) {
      setCreateStatus(i18n('structure.hub.dry.bulk_required',
        'Preview and confirm a bulk/substrate source first.'), true);
      return false;
    }
    let requests;
    try { requests = buildSurfaceRequests(); } catch (error) {
      setCreateStatus(error.message || String(error), true);
      return false;
    }
    invalidateDryRun(i18n('structure.hub.dry.running', 'Generating dry-run…'));
    const out = await VCS.call('surface_dry_run', SourceHubState.sourceTokens.bulk,
      requests.slabRequest, requests.siteRequest);
    const dryRun = out && out.dry_run;
    const operationToken = dryRun && dryRun.operation_token
      ? dryRun.operation_token : (out && out.operation_token);
    if (!out || out.error || out.ok === false || !dryRun ||
        dryRun.scientific_status !== 'candidate' || typeof operationToken !== 'string') {
      invalidateDryRun('');
      if (dryRun && dryRun.scientific_status === 'candidate') renderDryRun(dryRun);
      setCreateStatus(i18n('structure.hub.dry.failed', 'Dry-run failed closed: {error}', {
        error: (out && out.error) || i18n('structure.hub.dry.invalid_contract',
          'server did not return a candidate-bound operation token'),
      }), true);
      return false;
    }
    SourceHubState.dryRun = dryRun;
    SourceHubState.operationToken = operationToken;
    renderDryRun(dryRun);
    const output = $('surface-output-select');
    if (output) output.disabled = false;
    setCreateStatus(i18n('structure.hub.dry.review',
      'Review the dry-run, then choose a destination and explicitly confirm candidate creation.'), false);
    return true;
  }

  async function selectSurfaceOutput() {
    if (!SourceHubState.operationToken) return false;
    const out = await VCS.call('structure_output_select');
    const token = out && (out.output_selection_token || out.output_token || out.token);
    if (!out || out.cancelled) return false;
    if (out.error || out.ok === false || typeof token !== 'string' || !token) {
      setCreateStatus(i18n('structure.hub.output.failed',
        'Destination selection failed: {error}', {
          error: (out && out.error) || i18n('structure.hub.output.invalid_token',
            'server did not return an opaque output token'),
        }), true);
      return false;
    }
    SourceHubState.outputSelectionToken = token;
    SourceHubState.outputDisplayName = publicText(out.display_name ||
      (out.selection && out.selection.label) ||
      i18n('structure.hub.output.selected', 'Destination selected'));
    const name = $('surface-output-name');
    const ack = $('surface-candidate-ack');
    if (name) name.textContent = SourceHubState.outputDisplayName;
    if (ack) { ack.disabled = false; ack.focus(); }
    setCreateStatus(i18n('structure.hub.output.ready',
      'Destination token bound. Check the confirmation box to enable candidate creation.'), false);
    return true;
  }

  function syncCandidateCreateButton() {
    const ack = $('surface-candidate-ack');
    const create = $('surface-create-candidates');
    if (create) create.disabled = !(ack && ack.checked && SourceHubState.operationToken &&
      SourceHubState.outputSelectionToken && !SourceHubState.created);
  }

  async function createSurfaceCandidates() {
    const ack = $('surface-candidate-ack');
    if (!SourceHubState.operationToken || !SourceHubState.outputSelectionToken ||
        !ack || !ack.checked || SourceHubState.created) return false;
    const button = $('surface-create-candidates');
    if (button) button.disabled = true;
    const out = await VCS.call('surface_create_candidates', SourceHubState.operationToken,
      SourceHubState.outputSelectionToken);
    if (!out || out.error || out.ok === false ||
        (out.scientific_status && out.scientific_status !== 'candidate')) {
      syncCandidateCreateButton();
      setCreateStatus(i18n('structure.hub.create.failed',
        'Candidate creation failed: {error}', {
          error: (out && out.error) || i18n('structure.hub.create.invalid_status',
            'server returned a non-candidate scientific status'),
        }), true);
      return false;
    }
    SourceHubState.created = true;
    ack.disabled = true;
    const count = Array.isArray(out.candidate_ids) ? out.candidate_ids.length
      : (Array.isArray(out.jobs) ? out.jobs.length
        : (out.created_count == null ? '—' : out.created_count));
    setCreateStatus(i18n('structure.hub.create.done',
      'Created {count} candidate job(s). Nothing was submitted; scientific status remains candidate.', {
        count,
      }), false);
    return true;
  }

  function onSourceResultClick(event) {
    const button = event.target.closest('[data-source-preview][data-source-token]');
    if (!button) return;
    previewSource(button.dataset.sourcePreview, button.dataset.sourceToken);
  }

  function initSourceHub() {
    if (!$('structure-source-hub')) return;
    wire('src-bulk-local', () => selectLocalSource('bulk'));
    wire('src-ads-local', () => selectLocalSource('ads'));
    wire('src-ads-clear', clearAdsorbateSource);
    wire('src-bulk-confirm', () => confirmSource('bulk'));
    wire('src-ads-confirm', () => confirmSource('ads'));
    wire('source-search', searchSourceGateway);
    wire('surface-dry-run', runSurfaceDryRun);
    wire('surface-output-select', selectSurfaceOutput);
    wire('surface-create-candidates', createSurfaceCandidates);
    const ack = $('surface-candidate-ack');
    if (ack) ack.addEventListener('change', syncCandidateCreateButton);
    ['bulk', 'ads'].forEach(role => {
      const box = $(sourceRoleIds(role).results);
      if (box) box.addEventListener('click', onSourceResultClick);
    });
    document.querySelectorAll('#surface-wizard input, #surface-wizard select').forEach(control => {
      if (control.id === 'surface-candidate-ack') return;
      control.addEventListener('change', () => {
        if (SourceHubState.operationToken) invalidateDryRun(i18n(
          'structure.hub.dry.stale', 'Parameters changed; generate a new dry-run.'));
      });
    });
    loadSourceCapabilities();
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
    wire('ed-reset', resetView);
    wire('ed-bg', toggleBg);
    wire('ed-labels', toggleLabels);
    wire('ed-hydrogens', toggleHydrogens);
    wire('ed-paste-xyz', pasteXyz);
    wire('ed-measure', toggleMeasure);
    wire('ed-img', saveImage);
    wire('ed-selall', selectAll);
    wire('ed-selclear', clearSel);
    wire('ed-seldel', delSelected);
    wire('ed-atomlist-toggle', toggleAtomList);
    const styleSel = $('ed-style');
    if (styleSel) styleSel.addEventListener('change', () => setStyle(styleSel.value));
    document.querySelectorAll('#page-structure [data-view]').forEach(b =>
      b.addEventListener('click', () => applyView(b.dataset.view)));
    if (sel) sel.addEventListener('change', changeElem);
    document.querySelectorAll('#ed-edit [data-mv]').forEach(b => b.addEventListener('click', () => {
      const spec = b.dataset.mv;       // 形如 '0-'(x-)/'2+'(z+)
      move(parseInt(spec[0], 10), spec[1] === '-' ? -1 : 1);
    }));
    const alBody = $('ed-atomlist-body');
    if (alBody) alBody.addEventListener('click', e => {
      const tr = e.target.closest('tr[data-atom]');
      if (!tr) return;
      const i = parseInt(tr.dataset.atom, 10);
      State.selected = new Set([i]);
      selectAtom(i);
    });
    loadMolecules();
    initSourceHub();
  }

  // 首次进入结构页时初始化(3Dmol 容器需可见才能正确测量)
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'structure') { init(); setTimeout(render3D, 40); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  document.addEventListener('vcs:language', () => {
    renderProviderOptions();
    ['bulk', 'ads'].forEach(role => {
      showSourceResults(role, SourceHubState.results[role]);
      if (SourceHubState.previews[role]) renderSourcePreview(role, SourceHubState.previews[role]);
    });
    if (SourceHubState.dryRun) renderDryRun(SourceHubState.dryRun);
  });

  if (window.__VCS_TEST__) {
    window.__VCS_STRUCTURE_SOURCE_HUB_TEST__ = {
      state: SourceHubState,
      normalizeProviders,
      showSourceResults,
      previewSource,
      confirmSource,
      selectLocalSource,
      searchSourceGateway,
      clearAdsorbateSource,
      buildSurfaceRequests,
      runSurfaceDryRun,
      selectSurfaceOutput,
      createSurfaceCandidates,
      syncCandidateCreateButton,
      initSourceHub,
    };
  }

  // 供 molbuild.js(分子建模区)载入 3D 结构 / 取当前状态 / 清空 / 撤回
  window.Editor = {
    reload: loadMolecules,
    loadStruct: (struct, vacuum, path) => { init(); setStruct(struct, vacuum, path); },
    getStruct: () => (State.struct ? clone(State.struct) : null),
    getPath: () => State.path,
    setPath: p => { State.path = p; },
    clear: () => {
      State.struct = null; State.sel = null; State.selected = new Set();
      State.measure.picks = []; State.undo = [];
      const empty = $('ed-empty');
      if (empty) { empty.hidden = false; empty.textContent = '已清空;加载或建模后可编辑'; }
      if (viewer) { try { viewer.clear(); viewer.removeAllLabels(); viewer.render(); } catch (e) { /* 忽略 */ } }
      updateVacuum(null); updateFixState(); selectAtom(null);
    },
    undo,
  };
})();
