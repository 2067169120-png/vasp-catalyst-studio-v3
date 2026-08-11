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
  }

  // 首次进入结构页时初始化(3Dmol 容器需可见才能正确测量)
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'structure') { init(); setTimeout(render3D, 40); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

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
