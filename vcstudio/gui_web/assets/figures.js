// figures.js — 论文出图页(⑤):图表预设画廊(按分类分区)+ 侧滑详情(数据来源 + 参数表单)
// → render_figure_preset 一键出图。只依赖 app.js 的 VCS.* 与 api 桥。全部插值走 VCS.esc;
// 零 emoji;中文文案。缺数据的图型后端返回中文 skipped,前端 logFig 展示不假装出图。
'use strict';
(function () {
  const $ = id => document.getElementById(id);

  // 图表预设 key → 场景图型短键(供场景 figure_preset_order 白名单过滤);无映射者恒显示
  const PRESET_TO_SCENE = {
    adsorption_bar: 'bar', energy_matrix_table: 'table', free_energy_ladder: 'ladder',
    free_energy_ladder_multi: 'ladder', delta_e_heatmap: 'heatmap',
    scaling_relation: 'scaling', volcano: 'volcano', pdos: 'pdos',
    charge_profile: 'charge_profile',
  };
  const LADDER_KEYS = ['free_energy_ladder', 'free_energy_ladder_multi'];

  const State = { presets: [], categories: [], projects: [], reactions: [], current: null };

  // ── 项目下拉(数据来源) ──
  async function loadProjects() {
    const sel = $('fig-project');
    const r = await VCS.call('proj_list');
    State.projects = (r && r.projects) || [];
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = '';
    if (!State.projects.length) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = '(暂无项目)';
      sel.appendChild(o);
      return;
    }
    State.projects.forEach(p => {
      const o = document.createElement('option');
      o.value = p.path;
      o.textContent = (p.name || '(未命名)') + '(' + p.n_members + ' 成员)';
      sel.appendChild(o);
    });
    if (State.projects.some(p => p.path === prev)) sel.value = prev;
  }

  async function loadReactions() {
    const r = await VCS.call('reaction_presets');
    State.reactions = (r && r.presets) || [];
  }

  // ── 画廊:按分类分区渲染缩略卡片(场景 figure_preset_order 过滤) ──
  function sceneAllows(key) {
    const sc = VCS.scenario;
    const sk = PRESET_TO_SCENE[key];
    if (!sc || !sk) return true;                 // 无场景或无映射 → 显示
    return (sc.figure_preset_order || []).indexOf(sk) >= 0;
  }

  async function loadGallery() {
    const box = $('fig-gallery');
    if (!box) return;
    if (!State.presets.length) {
      const r = await VCS.call('figure_presets');
      if (!r || r.ok === false) { box.innerHTML = '<div class="db-empty">读取预设失败</div>'; return; }
      State.presets = r.presets || [];
      State.categories = r.categories || [];
    }
    renderGallery();
  }

  function renderGallery() {
    const box = $('fig-gallery');
    if (!box) return;
    let h = '';
    State.categories.forEach(cat => {
      const items = State.presets.filter(p => p.category === cat && sceneAllows(p.key));
      if (!items.length) return;
      h += `<div class="fig-cat"><div class="fig-cat-h">${VCS.esc(cat)}</div><div class="fig-grid">`;
      items.forEach(p => {
        h += `<div class="fig-card" data-key="${VCS.esc(p.key)}">` +
          `<div class="fig-thumb">${p.thumbnail_svg || ''}</div>` +
          `<b>${VCS.esc(p.name)}</b>` +
          `<div class="fig-desc">${VCS.esc(p.description || '')}</div></div>`;
      });
      h += '</div></div>';
    });
    box.innerHTML = h || '<div class="db-empty">当前研究场景未启用任何图型</div>';
    box.querySelectorAll('.fig-card').forEach(c =>
      c.addEventListener('click', () => openDrawer(c.dataset.key)));
  }

  // ── 侧滑详情:数据来源说明 + 参数表单(schema 生成)+ 生成 ──
  function openDrawer(key) {
    const preset = State.presets.find(p => p.key === key);
    if (!preset) return;
    State.current = preset;
    $('fig-drawer-title').textContent = preset.name;
    const body = $('fig-drawer-body');
    let h = `<div class="fig-need"><b>该图需要:</b>${VCS.esc(preset.required_data || '—')}</div>`;
    h += `<div class="fig-desc" style="margin-bottom:14px">${VCS.esc(preset.description || '')}</div>`;
    // 台阶图额外提供反应预设选择(数据来源之一)
    if (LADDER_KEYS.indexOf(key) >= 0) {
      h += '<div class="fig-param"><label>反应预设(空 = Li-S 放电默认)</label>' +
        '<select class="ipt" data-ctl="reaction_preset"><option value="">Li-S 放电(默认)</option>' +
        State.reactions.map(rp =>
          `<option value="${VCS.esc(rp.key)}">${VCS.esc(rp.description || rp.name || rp.key)}</option>`
        ).join('') + '</select></div>';
    }
    // params_schema → 表单
    const schema = preset.params_schema || {};
    Object.keys(schema).forEach(k => {
      const def = schema[k];
      h += '<div class="fig-param"><label>' + VCS.esc(k) + '</label>' + paramInput(k, def) + '</div>';
    });
    body.innerHTML = h;
    $('fig-drawer').hidden = false;
  }

  function paramInput(k, def) {
    if (typeof def === 'boolean') {
      return `<label style="flex-direction:row;gap:6px"><input type="checkbox" data-p="${VCS.esc(k)}"` +
        (def ? ' checked' : '') + '> 启用</label>';
    }
    if (typeof def === 'number') {
      return `<input class="ipt" type="text" inputmode="decimal" data-p="${VCS.esc(k)}" data-t="number" value="${VCS.esc(String(def))}">`;
    }
    if (Array.isArray(def)) {
      return `<input class="ipt" data-p="${VCS.esc(k)}" data-t="array" value="${VCS.esc(def.join(', '))}">`;
    }
    return `<input class="ipt" data-p="${VCS.esc(k)}" value="${VCS.esc(def == null ? '' : String(def))}">`;
  }

  function collectParams() {
    const body = $('fig-drawer-body');
    const params = {};
    body.querySelectorAll('[data-ctl]').forEach(el => {
      if (el.value) params[el.getAttribute('data-ctl')] = el.value;
    });
    body.querySelectorAll('[data-p]').forEach(el => {
      const k = el.getAttribute('data-p');
      const t = el.getAttribute('data-t');
      if (el.type === 'checkbox') { params[k] = el.checked; return; }
      const v = el.value.trim();
      if (v === '') return;
      if (t === 'number') { const n = parseFloat(v); if (!isNaN(n)) params[k] = n; }
      else if (t === 'array') params[k] = v.split(',').map(s => s.trim()).filter(Boolean);
      else params[k] = v;
    });
    return params;
  }

  function closeDrawer() { $('fig-drawer').hidden = true; }

  async function generate() {
    if (!State.current) return;
    const proj = $('fig-project') ? $('fig-project').value : '';
    if (!proj) { VCS.log('请先选择数据来源项目', 'failc'); return; }
    const params = collectParams();
    const btn = $('fig-drawer-gen');
    if (btn) btn.disabled = true;
    VCS.log('出图中(' + State.current.name + ')…');
    try {
      const r = await VCS.call('render_figure_preset', State.current.key, proj, params);
      logFig(r, State.current.name);
      if (r && (r.files || []).length) closeDrawer();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function logFig(r, what) {
    if (!r || r.ok === false || r.error) {
      VCS.log(what + '失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    (r.files || []).forEach(f => VCS.log('已生成:' + f, 'okc'));
    (r.skipped || []).forEach(s => VCS.log('跳过 ' + (s.kind || '') + ':' + s.reason, 'warnc'));
    if ((r.files || []).length) {
      VCS.log('图已输出到:' + r.out_dir, 'okc');
      VCS.call('open_dir', r.out_dir);
      VCS.toast('已生成 ' + r.files.length + ' 个文件');
    } else if ((r.skipped || []).length) {
      VCS.toast('该图缺数据(原因见日志)', 'fail');
    }
  }

  // ── 初始化 ──
  let inited = false;
  async function init() {
    if (inited) return; inited = true;
    const rf = $('fig-refresh'); if (rf) rf.addEventListener('click', loadProjects);
    const cl = $('fig-drawer-close'); if (cl) cl.addEventListener('click', closeDrawer);
    const mk = $('fig-drawer-mask'); if (mk) mk.addEventListener('click', closeDrawer);
    const gn = $('fig-drawer-gen'); if (gn) gn.addEventListener('click', generate);
    await loadReactions();
    await loadGallery();
    await loadProjects();
  }

  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'figures') { init(); loadProjects(); }
  });
  // 场景切换时重渲画廊(图型白名单变化)
  document.addEventListener('vcs:scenario', () => { if (State.presets.length) renderGallery(); });

  window.Figures = { reload: loadProjects };
})();
