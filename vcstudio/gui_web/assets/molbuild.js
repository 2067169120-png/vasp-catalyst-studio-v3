// molbuild.js — ①结构建模页·分子建模分区(图片识别 → SMILES → 3D 建模 → 外部编辑器往返)。
// 卡1 OCSR:图片→SMILES(mol_image_to_smiles)+ 2D SVG(mol_smiles_svg)。
// 卡2 3D 建模:SMILES→3D(mol_smiles_to_3d)载入编辑器;打开 xyz/mol(mol_reimport);
//     保存 xyz/mol/pdb(JS 端写出下载);下一步→②预选 Gaussian+分子并携带结构。
// 卡3 外部编辑器:导出并打开(mol_export_editor + mol_open_with)+ 导入(mol_reimport)+
//     mtime 轮询自动检测。只依赖 app.js 的 VCS.* 与 window.Editor;全部插值走 VCS.esc。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = (v == null ? '' : v); };

  const State = { editPath: null, editMtime: null, poll: null, toolPaths: {} };

  // ── 卡1:图片识别 OCSR ──
  async function ocsrProbe() {
    const banner = $('mol-ocsr-banner');
    if (!banner) return;
    const r = await VCS.call('mol_ocsr_probe');
    if (r && r.ok && r.available === false) {
      banner.hidden = false;
      banner.textContent = r.detail || '未安装 DECIMER,图片识别不可用';
    } else {
      banner.hidden = true;
    }
  }
  async function pickImage() {
    const r = await VCS.call('pick_file', 'image');
    if (r && r.error) { VCS.log('选择图片失败:' + r.error, 'failc'); return; }
    if (r && r.path) setVal('mol-img-path', r.path);
  }
  async function runOcsr() {
    const p = val('mol-img-path');
    if (!p) { VCS.log('请先选择分子结构图片', 'failc'); return; }
    const btn = $('mol-ocsr-run');
    if (btn) btn.disabled = true;
    VCS.log('图片识别(DECIMER)中…');
    try {
      const r = await VCS.call('mol_image_to_smiles', p);
      if (!r || r.ok === false || r.error) {
        VCS.log('图片识别失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
      }
      setVal('mol-img-smiles', r.smiles);
      VCS.log('识别出 SMILES:' + r.smiles + '(耗时 ' + r.elapsed_ms + ' ms)', 'okc');
      refreshSvg();
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  async function refreshSvg() {
    const s = val('mol-img-smiles');
    const box = $('mol-svg');
    if (!box) return;
    if (!s) { box.innerHTML = '<span class="sub">先识别或填入 SMILES</span>'; return; }
    const r = await VCS.call('mol_smiles_svg', s, 420, 300);
    if (!r || r.ok === false || r.error) {
      box.innerHTML = '<span class="sub">' + VCS.esc('2D 结构图失败:' + ((r && r.error) || '未知')) + '</span>';
      return;
    }
    box.innerHTML = r.svg;   // RDKit 生成的可信 SVG
  }
  async function copySmiles() {
    const s = val('mol-img-smiles');
    if (!s) { VCS.toast('无 SMILES 可复制', 'fail'); return; }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(s);
      else legacyCopy(s);
      VCS.toast('已复制 SMILES');
    } catch (e) { legacyCopy(s); VCS.toast('已复制 SMILES'); }
  }
  function legacyCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) { /* 忽略 */ }
    ta.remove();
  }
  function nextToBuild() {
    const s = val('mol-img-smiles');
    if (!s) { VCS.toast('请先识别或填入 SMILES', 'fail'); return; }
    setVal('mol-smiles', s);
    VCS.toast('已带入 3D 建模,点「生成 3D 结构」');
    const c = $('mol-smiles');
    if (c) c.focus();
  }

  // ── 卡2:分子 3D 建模 ──
  async function build3d() {
    const s = val('mol-smiles');
    if (!s) { VCS.log('请填入 SMILES 后再建模', 'failc'); return; }
    const ff = val('mol-ff') || 'auto';
    const btn = $('mol-3d-run');
    if (btn) btn.disabled = true;
    VCS.log('SMILES → 3D 建模(' + ff + ')…');
    try {
      const r = await VCS.call('mol_smiles_to_3d', s, ff);
      if (!r || r.ok === false || r.error) {
        VCS.log('3D 建模失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
      }
      if (window.Editor && window.Editor.loadStruct) window.Editor.loadStruct(r.struct, null, null);
      renderProps(r.struct, r.charge, r.multiplicity_hint);
      (r.warnings || []).forEach(w => VCS.log('  ⚠ ' + w, 'warnc'));
      VCS.log('已生成 3D 结构:' + r.struct.formula + '(' + r.struct.natoms + ' 原子)已载入编辑器', 'okc');
      VCS.toast('已生成 3D 结构');
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  async function openFile() {
    const r = await VCS.call('pick_file', 'xyz');
    if (r && r.error) { VCS.log('选择结构文件失败:' + r.error, 'failc'); return; }
    if (!r || !r.path) return;
    const out = await VCS.call('mol_reimport', r.path);   // xyz/mol 纯手写解析
    if (!out || out.ok === false || out.error) {
      VCS.log('打开结构文件失败:' + ((out && out.error) || '未知错误'), 'failc'); return;
    }
    if (window.Editor && window.Editor.loadStruct) window.Editor.loadStruct(out.struct, null, r.path);
    refreshProps();
    VCS.log('已打开结构文件:' + r.path + '(' + out.struct.formula + ')', 'okc');
  }
  async function refreshProps() {
    const st = window.Editor && window.Editor.getStruct ? window.Editor.getStruct() : null;
    if (!st) return;
    const r = await VCS.call('mol_info', st.elements, st.coords, 0);
    if (r && r.ok) renderProps(st, r.charge, r.suggested_multiplicity, r);
  }
  function renderProps(struct, charge, mult, info) {
    const box = $('mol-props');
    if (!box) return;
    const ne = info && info.n_electrons != null ? info.n_electrons : '—';
    const mass = info && info.mass_amu != null ? info.mass_amu : '—';
    box.innerHTML =
      `<span>分子式 <b>${VCS.esc(struct.formula || '—')}</b></span>` +
      `<span>原子数 <b>${struct.natoms != null ? struct.natoms : struct.elements.length}</b></span>` +
      `<span>电子数 <b>${VCS.esc(String(ne))}</b></span>` +
      `<span>分子量 <b>${VCS.esc(String(mass))}</b></span>` +
      `<span>电荷 <b>${VCS.esc(String(charge != null ? charge : 0))}</b></span>` +
      `<span>建议多重度 <b>${VCS.esc(String(mult != null ? mult : '—'))}</b></span>`;
  }
  function clearStruct() {
    if (window.Editor && window.Editor.clear) window.Editor.clear();
    const box = $('mol-props');
    if (box) box.innerHTML = '<span class="sub">生成或载入结构后显示:原子数 / 分子式 / 电子数 / 建议多重度</span>';
    VCS.log('已清空结构', 'okc');
  }
  function undoStruct() { if (window.Editor && window.Editor.undo) window.Editor.undo(); }

  // 保存 xyz/mol/pdb(JS 端写出 + 浏览器下载;pdb 简单 HETATM)
  function saveAs() {
    const st = window.Editor && window.Editor.getStruct ? window.Editor.getStruct() : null;
    if (!st || !st.elements.length) { VCS.log('无结构可保存', 'failc'); return; }
    const fmt = val('mol-save-fmt') || 'xyz';
    let text;
    if (fmt === 'xyz') text = toXyz(st);
    else if (fmt === 'mol') text = toMol(st);
    else text = toPdb(st);
    const blob = new Blob([text], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'molecule.' + fmt;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 100);
    VCS.log('已保存 molecule.' + fmt, 'okc');
    VCS.toast('已保存 ' + fmt.toUpperCase());
  }
  function toXyz(st) {
    const lines = [String(st.elements.length), 'vcstudio molecule'];
    for (let i = 0; i < st.elements.length; i++) {
      const c = st.coords[i];
      lines.push(`${st.elements[i]} ${(+c[0]).toFixed(6)} ${(+c[1]).toFixed(6)} ${(+c[2]).toFixed(6)}`);
    }
    return lines.join('\n') + '\n';
  }
  function toMol(st) {
    const n = st.elements.length;
    const pad3 = v => String(v).padStart(3, ' ');
    const lines = ['vcstudio', '  vcstudio', '',
      `${pad3(n)}  0  0  0  0  0  0  0  0  0999 V2000`];
    for (let i = 0; i < n; i++) {
      const c = st.coords[i];
      const f = v => (+v).toFixed(4).padStart(10, ' ');
      lines.push(`${f(c[0])}${f(c[1])}${f(c[2])} ${(st.elements[i] + '  ').slice(0, 3)}` +
        ' 0  0  0  0  0  0  0  0  0  0  0  0');
    }
    lines.push('M  END');
    return lines.join('\n') + '\n';
  }
  function toPdb(st) {
    const lines = [];
    for (let i = 0; i < st.elements.length; i++) {
      const c = st.coords[i];
      const el = st.elements[i];
      const serial = String(i + 1).padStart(5, ' ');
      const name = (el + '   ').slice(0, 4);
      const x = (+c[0]).toFixed(3).padStart(8, ' ');
      const y = (+c[1]).toFixed(3).padStart(8, ' ');
      const z = (+c[2]).toFixed(3).padStart(8, ' ');
      const sym = el.toUpperCase().padStart(2, ' ');
      lines.push(`HETATM${serial} ${name} MOL     1    ${x}${y}${z}  1.00  0.00          ${sym}`);
    }
    lines.push('END');
    return lines.join('\n') + '\n';
  }

  // 下一步:携当前结构去 ② 生成输入,预选 Gaussian + 分子
  function toGenerate() {
    const st = window.Editor && window.Editor.getStruct ? window.Editor.getStruct() : null;
    if (!st || !st.elements.length) { VCS.log('请先建模或载入结构', 'failc'); return; }
    const nav = document.querySelector('nav a[data-page="generate"]');
    if (nav) nav.click();
    if (window.Generate && typeof window.Generate.useMolecule === 'function') {
      window.Generate.useMolecule(st);
      VCS.toast('已带入 ② Gaussian 分子面板');
    } else {
      VCS.toast('已切到生成页;请选 Gaussian 引擎');
    }
  }

  // ── 卡3:外部编辑器往返 ──
  async function loadToolPaths() {
    const r = await VCS.call('tool_paths_get');
    State.toolPaths = (r && r.paths) || {};
    syncEditorPath();
  }
  function editorKey() {
    const sel = val('mol-editor-sel') || 'gaussview';
    return 'editor_' + sel;
  }
  function syncEditorPath() {
    setVal('mol-editor-path', State.toolPaths[editorKey()] || '');
  }
  async function saveEditorPath() {
    const kv = {}; kv[editorKey()] = val('mol-editor-path');
    const r = await VCS.call('tool_paths_set', kv);
    if (r && r.paths) State.toolPaths = r.paths;
  }
  async function exportAndOpen() {
    const st = window.Editor && window.Editor.getStruct ? window.Editor.getStruct() : null;
    if (!st || !st.elements.length) { VCS.log('无结构可导出', 'failc'); return; }
    const fmt = val('mol-editor-fmt') || 'xyz';
    const r = await VCS.call('mol_export_editor', st.elements, st.coords, fmt, null);
    if (!r || r.ok === false || r.error) {
      VCS.log('导出编辑文件失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    State.editPath = r.path;
    State.editMtime = r.mtime;
    await saveEditorPath();
    const exe = val('mol-editor-path') || null;
    const o = await VCS.call('mol_open_with', r.path, exe);
    if (!o || o.ok === false || o.error) {
      VCS.log('打开外部编辑器失败:' + ((o && o.error) || '未知错误') + '(编辑文件已导出:' + r.path + ')', 'warnc');
    } else {
      VCS.log('已导出并用外部编辑器打开:' + r.path, 'okc');
    }
    setImportReady(false);
    startPoll();
  }
  async function importEdit() {
    if (!State.editPath) { VCS.log('请先「导出并打开」再导入', 'failc'); return; }
    const r = await VCS.call('mol_reimport', State.editPath);
    if (!r || r.ok === false || r.error) {
      VCS.log('导入编辑结果失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    if (window.Editor && window.Editor.loadStruct) window.Editor.loadStruct(r.struct, null, null);
    refreshProps();
    setImportReady(false);
    VCS.log('已导入外部编辑结果:' + r.struct.formula + '(' + r.struct.natoms + ' 原子)', 'okc');
    VCS.toast('已导入编辑结果');
  }
  function setImportReady(ready) {
    const btn = $('mol-editor-import');
    if (btn) btn.classList.toggle('ready', ready);
    const hint = $('mol-reimport-hint');
    if (hint) {
      hint.textContent = ready
        ? '检测到外部编辑改动 — 点「导入编辑结果」回读到编辑器。'
        : '导出后自动检测外部改动;检测到变化即高亮「导入编辑结果」。';
      hint.classList.toggle('hot', ready);
    }
  }
  function startPoll() {
    stopPoll();
    State.poll = setInterval(async () => {
      if (!State.editPath) return;
      const r = await VCS.call('mol_check_reimport', State.editPath, State.editMtime);
      if (r && r.ok && r.changed) {
        State.editMtime = r.mtime;
        setImportReady(true);
      }
    }, 2500);
  }
  function stopPoll() { if (State.poll) { clearInterval(State.poll); State.poll = null; } }

  // ── 溶剂化复合物(核 + 显式溶剂盒)──
  async function loadSolventPresets() {
    const sel = $('solv-preset');
    if (!sel) return;
    const r = await VCS.call('solvent_presets');
    const presets = (r && r.presets) || [];
    sel.innerHTML = presets.map(p => `<option value="${VCS.esc(p.key)}">${VCS.esc(p.label)}(${VCS.esc(p.key)})</option>`).join('');
  }
  function solvArgs() {
    return {
      core: ($('solv-core') && $('solv-core').value) || 'Li2S3',
      preset: ($('solv-preset') && $('solv-preset').value) || 'lis_electrolyte',
      box: parseFloat(($('solv-box') && $('solv-box').value) || '18') || 18,
    };
  }
  async function buildSolvated(save) {
    const a = solvArgs();
    let saveTo = null;
    if (save) {
      const d = await VCS.call('pick_dir');
      if (!d || !d.path) return;
      const sep = d.path.indexOf('\\') >= 0 ? '\\' : '/';
      saveTo = d.path + sep + a.core + '_solvated.vasp';
    }
    VCS.log('组装溶剂化复合物(' + a.core + ' + ' + a.preset + ')…');
    const r = await VCS.call('build_solvated', a.core, a.preset, a.box, 2.5, 42, saveTo);
    const out = $('solv-out');
    if (!r || r.ok === false || r.error) {
      VCS.log('溶剂化组装失败:' + ((r && r.error) || '未知'), 'failc');
      if (out) out.innerHTML = `<span class="sub" style="color:var(--fail)">${VCS.esc((r && r.error) || '失败')}</span>`;
      return;
    }
    if (out) out.innerHTML = `<b>${r.n_atoms}</b> 原子 · ${VCS.esc(r.note || '')}`;
    if (saveTo) { VCS.log('已保存:' + r.saved_to, 'okc'); VCS.toast('已保存溶剂化 POSCAR'); return; }
    // 载入编辑器(经临时 POSCAR → struct_load)
    if (r.temp_path) {
      const sr = await VCS.call('struct_load', r.temp_path);
      if (sr && sr.ok && sr.struct && window.Editor && window.Editor.loadStruct) {
        window.Editor.loadStruct(sr.struct, null, null);
        VCS.log('溶剂化复合物已载入编辑器:' + r.n_atoms + ' 原子', 'okc');
        VCS.toast('已载入编辑器');
      } else {
        VCS.log('已组装 ' + r.n_atoms + ' 原子(载入编辑器失败,可点保存 POSCAR)', 'warnc');
      }
    }
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    wire('solv-build', () => buildSolvated(false));
    wire('solv-save', () => buildSolvated(true));
    loadSolventPresets();
    wire('mol-img-btn', pickImage);
    wire('mol-ocsr-run', runOcsr);
    wire('mol-svg-refresh', refreshSvg);
    wire('mol-img-copy', copySmiles);
    wire('mol-img-next', nextToBuild);
    wire('mol-3d-run', build3d);
    wire('mol-open-file', openFile);
    wire('mol-3d-clear', clearStruct);
    wire('mol-3d-undo', undoStruct);
    wire('mol-save-btn', saveAs);
    wire('mol-to-generate', toGenerate);
    wire('mol-editor-export', exportAndOpen);
    wire('mol-editor-import', importEdit);
    wire('mol-editor-path-btn', async () => {
      const r = await VCS.call('pick_file', 'exe');
      if (r && r.path) { setVal('mol-editor-path', r.path); saveEditorPath(); }
    });
    const esel = $('mol-editor-sel');
    if (esel) esel.addEventListener('change', syncEditorPath);
    const epath = $('mol-editor-path');
    if (epath) epath.addEventListener('change', saveEditorPath);
    ocsrProbe();
    loadToolPaths();
  }

  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'structure') { init(); ocsrProbe(); }
    else stopPoll();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.MolBuild = { reprobe: ocsrProbe };
})();
