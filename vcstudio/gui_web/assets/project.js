// project.js — 吸附能项目页:新建项目(清洁表面 + 组态族 + 气相参考 → 批量生成)
// + 已有项目 ΔE 汇总 / 导出 CSV / 生成完整报告。行为对齐 vcstudio/gui/project_tab.py。
// 只依赖 app.js 暴露的 VCS.* 与 api 桥方法(proj_*/pick_file/pick_dir)。
// 全部插值走 VCS.esc;零 emoji;中文文案。数字列用 td.num 右对齐等宽。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = v || ''; };

  const State = {
    configs: [],      // 组态 POSCAR 路径列表(逐个添加)
    projects: [],     // proj_list 返回:[{path,name,n_members}]
  };

  // 目录 + 默认文件名 → 完整保存路径(按目录内的分隔符风格拼接,兼容 Windows/Posix)
  function joinPath(dir, filename) {
    const d = String(dir).replace(/[\\/]+$/, '');
    const sep = d.indexOf('\\') >= 0 ? '\\' : '/';
    return d + sep + filename;
  }

  // ── 新建项目:文件/目录选择 ────────────────────────────────────────────────
  async function pickInto(id, kind) {
    const r = await VCS.call('pick_file', kind);
    if (r && r.error) { VCS.log('选择文件失败:' + r.error, 'failc'); return; }
    if (r && r.path) setVal(id, r.path);
  }
  async function pickDirInto(id) {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log('选择目录失败:' + r.error, 'failc'); return; }
    if (r && r.path) setVal(id, r.path);
  }

  // ── 组态列表:逐个添加 + 可移除 ────────────────────────────────────────────
  function renderConfigs() {
    const box = $('pj-cfg-list');
    if (!box) return;
    if (!State.configs.length) {
      box.innerHTML = '<div class="pj-cfgempty">尚未添加任何吸附组态</div>';
      return;
    }
    box.innerHTML = State.configs.map((p, i) =>
      `<div class="pj-cfgrow"><span class="path" title="${VCS.esc(p)}">${VCS.esc(p)}</span>` +
      `<button class="btn quiet" data-rm="${i}">移除</button></div>`).join('');
    box.querySelectorAll('button[data-rm]').forEach(btn => {
      btn.addEventListener('click', () => {
        State.configs.splice(+btn.dataset.rm, 1);
        renderConfigs();
      });
    });
  }

  async function addConfig() {
    const r = await VCS.call('pick_file', 'poscar');
    if (r && r.error) { VCS.log('选择组态失败:' + r.error, 'failc'); return; }
    if (r && r.path) {
      if (State.configs.indexOf(r.path) >= 0) {
        VCS.log('该组态已在列表中,已跳过:' + r.path);
        return;
      }
      State.configs.push(r.path);
      renderConfigs();
    }
  }

  // ── 批量生成:proj_create → advisories/warnings 逐条 warn 级 log → 成功刷台账 ──
  async function create() {
    const btn = $('pj-create');
    const name = val('pj-name'), slab = val('pj-slab'), incar = val('pj-incar');
    const gas = val('pj-gas'), root = val('pj-root');
    const configs = State.configs.slice();
    if (btn) btn.disabled = true;
    VCS.log('批量生成:清洁表面 + ' + configs.length + ' 组态' + (gas ? ' + 气相参考' : '') + '…');
    try {
      const r = await VCS.call('proj_create', name, slab, configs, incar, gas, root);
      if (!r || r.ok === false || r.error) {
        VCS.log('批量生成失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      // 后端已把 advisories 与 warnings 拍平为字符串列表;都用 warn 级 log
      (r.advisories || []).forEach(a => VCS.log('方法学提示:' + a, 'warnc'));
      (r.warnings || []).forEach(w => VCS.log(w, 'warnc'));
      VCS.log('已生成项目:' + (r.project_path || ''), 'okc');
      VCS.log('作业已入台账,去任务页上传提交;全部 DONE 后回本页算 ΔE', 'okc');
      // 生成的成员作业进了台账 → 刷新任务页
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      await reloadProjects();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 已有项目:下拉 + 刷新 ──────────────────────────────────────────────────
  async function reloadProjects() {
    const sel = $('pj-select');
    const r = await VCS.call('proj_list');
    State.projects = (r && r.projects) || [];
    if (r && r.error) VCS.log('读取项目列表失败:' + r.error, 'failc');
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
    const want = State.projects.some(p => p.path === prev) ? prev
      : State.projects[State.projects.length - 1].path;
    sel.value = want;
    renderFigProjList();
  }

  // ── 论文级出图:多项目对比勾选列表(随项目列表刷新) ──────────────────────
  function renderFigProjList() {
    const box = $('fig-projlist');
    if (!box) return;
    box.innerHTML = '';
    if (!State.projects.length) {
      box.textContent = '(暂无项目)';
      return;
    }
    State.projects.forEach(p => {
      const lab = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.path = p.path;
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(p.name || '(未命名)'));
      box.appendChild(lab);
    });
  }

  function logFigResult(r, what) {
    if (!r || r.ok === false || r.error) {
      VCS.log(what + '失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    (r.files || []).forEach(f => VCS.log('已生成:' + f, 'okc'));
    (r.skipped || []).forEach(s => VCS.log('跳过 ' + s.kind + ':' + s.reason, 'warnc'));
    if ((r.files || []).length) {
      VCS.log('图已输出到:' + r.out_dir, 'okc');
      VCS.call('open_dir', r.out_dir);          // 生成即可看
      VCS.toast('已生成 ' + r.files.length + ' 个文件');
    } else if ((r.skipped || []).length) {
      VCS.toast('本次没有可生成的图(原因见日志)', 'fail');
    }
  }

  // 当前项目出图:按勾选的图类型调 proj_figures
  async function makeFigures() {
    const proj = currentProject();
    if (!proj) return;
    const kinds = [];
    if ($('fig-bar') && $('fig-bar').checked) kinds.push('bar');
    if ($('fig-table') && $('fig-table').checked) kinds.push('table');
    if ($('fig-ladder') && $('fig-ladder').checked) kinds.push('ladder');
    if (!kinds.length) { VCS.log('请至少勾选一种图', 'failc'); return; }
    const btn = $('pj-figs');
    if (btn) btn.disabled = true;
    VCS.log('出图中(' + kinds.join('/') + ')…');
    try {
      const r = await VCS.call('proj_figures', proj.path, kinds, null);
      logFigResult(r, '出图');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // 多项目对比出图:勾选的项目 + 勾选的图类型调 proj_compare_figures
  async function makeCompareFigures() {
    const box = $('fig-projlist');
    const paths = box
      ? Array.from(box.querySelectorAll('input:checked')).map(cb => cb.dataset.path)
      : [];
    if (paths.length < 2) { VCS.log('多项目对比请勾选至少 2 个项目', 'failc'); return; }
    const kinds = [];
    if ($('fig-heatmap') && $('fig-heatmap').checked) kinds.push('heatmap');
    if ($('fig-scaling') && $('fig-scaling').checked) kinds.push('scaling');
    if ($('fig-volcano') && $('fig-volcano').checked) kinds.push('volcano');
    if (!kinds.length) { VCS.log('请至少勾选一种对比图', 'failc'); return; }
    const btn = $('pj-cmpfigs');
    if (btn) btn.disabled = true;
    VCS.log('对比出图中(' + paths.length + ' 个项目,' + kinds.join('/') + ')…');
    try {
      const r = await VCS.call('proj_compare_figures', paths, kinds, null);
      logFigResult(r, '对比出图');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function currentProject() {
    const sel = $('pj-select');
    const path = sel ? sel.value : '';
    if (!path) { VCS.log('请先选择一个项目', 'failc'); return null; }
    return State.projects.find(p => p.path === path) || { path, name: '' };
  }

  // ── 计算 ΔE:proj_delta → 表格渲染(缺员门控原样展示,绝不编数) ──────────
  async function delta() {
    const proj = currentProject();
    if (!proj) return;
    const box = $('pj-table');
    if (box) box.innerHTML = '';
    VCS.log('计算项目「' + (proj.name || '') + '」的 ΔE…');
    const r = await VCS.call('proj_delta', proj.path);
    if (!r || r.ok === false || r.error) {
      VCS.log('计算 ΔE 失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    renderDelta(r);
  }

  function fmt(x, digits) {
    return (typeof x === 'number' && isFinite(x)) ? x.toFixed(digits) : '—';
  }

  function renderDelta(r) {
    const box = $('pj-table');
    if (!box) return;
    const rows = r.rows || [];
    const note = r.note || '';
    let h = note ? `<div class="pj-note">${VCS.esc(note)}</div>` : '';
    if (!rows.length) {
      h += '<div class="empty"><p>该项目暂无吸附组态成员</p></div>';
      box.innerHTML = h;
      return;
    }
    h += '<table><thead><tr>' +
      '<th>组态</th><th>状态</th><th class="num">E_config (eV)</th>' +
      '<th class="num">ΔE (eV)</th><th>备注</th>' +
      '</tr></thead><tbody>';
    rows.forEach(row => {
      h += '<tr>' +
        `<td><span class="name">${VCS.esc(row.name)}</span></td>` +
        `<td>${VCS.pill(row.state)}</td>` +
        `<td class="num">${VCS.esc(fmt(row.e_config, 6))}</td>` +
        `<td class="num">${row.delta_e == null ? '—' : VCS.esc(fmt(row.delta_e, 4))}</td>` +
        `<td class="sub">${VCS.esc(row.note || '')}</td></tr>`;
    });
    h += '</tbody></table>';
    box.innerHTML = h;
  }

  // ── 导出 CSV:pick_dir + 默认文件名 → proj_export_csv ──────────────────────
  async function exportCsv() {
    const proj = currentProject();
    if (!proj) return;
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;   // 用户取消
    const save = joinPath(dr.path, (proj.name || 'project') + '_delta_e.csv');
    VCS.log('导出 ΔE 表到:' + save + '…');
    const r = await VCS.call('proj_export_csv', proj.path, save);
    if (!r || r.ok === false || r.error) {
      VCS.log('导出 CSV 失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    VCS.log('已导出 CSV:' + (r.file || save), 'okc');
    VCS.call('open_dir', r.file || save);       // 输出反馈统一:打开所在目录
    VCS.toast('已导出 CSV');
  }

  // ── 生成完整报告:pick_dir + 默认文件名 → proj_report(耗时长,按钮禁用) ──
  async function report() {
    const proj = currentProject();
    if (!proj) return;
    const dr = await VCS.call('pick_dir');
    if (dr && dr.error) { VCS.log('选择目录失败:' + dr.error, 'failc'); return; }
    if (!dr || !dr.path) return;   // 用户取消
    const save = joinPath(dr.path, (proj.name || 'project') + '_完整报告.html');
    const btn = $('pj-report');
    if (btn) btn.disabled = true;
    VCS.log('生成完整报告(Origin 图表 + 结构图 + AI 分析),生成中,可能需要几分钟…');
    try {
      const r = await VCS.call('proj_report', proj.path, save);
      if (!r || r.ok === false || r.error) {
        VCS.log('生成完整报告失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      VCS.log('完整报告已生成:' + (r.file || save), 'okc');
      VCS.call('open_dir', r.file || save);     // 输出反馈统一:打开所在目录
      VCS.toast('报告已生成');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 初始化 ─────────────────────────────────────────────────────────────────
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }

  function init() {
    wire('pj-slab-btn', () => pickInto('pj-slab', 'poscar'));
    wire('pj-incar-btn', () => pickInto('pj-incar', 'incar'));
    wire('pj-gas-btn', () => pickInto('pj-gas', 'poscar'));
    wire('pj-root-btn', () => pickDirInto('pj-root'));
    wire('pj-cfg-add', addConfig);
    wire('pj-create', create);
    wire('pj-refresh', reloadProjects);
    wire('pj-delta', delta);
    wire('pj-csv', exportCsv);
    wire('pj-report', report);
    wire('pj-figs', makeFigures);
    wire('pj-cmpfigs', makeCompareFigures);
    renderConfigs();
    reloadProjects();
  }

  // 任务页组头「算 ΔE」调用:刷新项目列表后按项目名选中(找不到则保持默认)
  async function selectByName(name) {
    await reloadProjects();
    const hit = State.projects.find(p => p.name === name);
    const sel = $('pj-select');
    if (hit && sel) sel.value = hit.path;
  }

  // 切回项目页时刷新项目下拉
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'project') reloadProjects();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Project = { reload: reloadProjects, selectByName };
})();
