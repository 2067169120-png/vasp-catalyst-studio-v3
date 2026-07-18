// generate.js — 生成页:POSCAR/INCAR/输出目录/赝势库四行(文本框+浏览)+ 即时解析预览
// + 「生成四件套」一键生成。行为对齐 vcstudio/gui/generate_tab.py:
// 启动 gen_state 回填最近路径;POSCAR+INCAR 齐备即调 gen_preview 渲染到 <pre class="mono">;
// gen_run 成功后逐条 log warnings + 提示去任务页,并刷新任务页台账。
// 只依赖 app.js 暴露的 VCS.*;所有插值走 VCS.esc(pre 用 textContent 天然安全);零 emoji;中文文案。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = v || ''; };
  const State = { previewKey: null };   // 预览去重 + 防旧响应覆盖

  // ── 即时预览:POSCAR+INCAR 都非空时调 gen_preview,渲染结构化 summary 为多行文本 ──
  async function refreshPreview() {
    const pre = $('gen-preview');
    if (!pre) return;
    const poscar = val('gen-poscar'), incar = val('gen-incar');
    const calc = val('gen-calc') || 'slab';
    // 预览区改为可编辑 textarea(用 .value);setTxt 兼容 <pre>/<textarea>
    const setTxt = t => { if ('value' in pre) pre.value = t; else pre.textContent = t; };
    if (!poscar || !incar) {
      State.previewKey = null;
      pre.style.color = '';
      setTxt('(选择 POSCAR / INCAR 后自动解析预览)');
      return;
    }
    const key = poscar + '\n' + incar + '\n' + calc;
    if (key === State.previewKey) return;   // 相同输入不重复解析(镜像 _preview_memo)
    State.previewKey = key;
    pre.style.color = '';
    setTxt('正在解析…');
    const r = await VCS.call('gen_preview', poscar, incar, calc);
    if (State.previewKey !== key) return;   // 期间用户又改了路径 → 丢弃旧响应
    if (!r || r.ok === false || r.error) {
      pre.style.color = 'var(--fail)';
      setTxt('预览失败:' + ((r && r.error) || '未知错误'));
      return;
    }
    const s = r.summary || {};
    pre.style.color = '';
    setTxt([s.poscar, s.incar].filter(Boolean).join('\n\n') || '(无预览内容)');
  }

  // 保存预览到文件(可编辑预览区内容 → save_text)
  async function savePreview() {
    const pre = $('gen-preview');
    const txt = pre ? ('value' in pre ? pre.value : pre.textContent) : '';
    if (!txt || !txt.trim()) { VCS.log('预览为空,无内容可保存', 'failc'); return; }
    const d = await VCS.call('pick_dir');
    if (!d || !d.path) return;
    const sep = d.path.indexOf('\\') >= 0 ? '\\' : '/';
    const dest = d.path + sep + 'preview.txt';
    const r = await VCS.call('save_text', dest, txt);
    if (r && r.ok) { VCS.log('预览已保存:' + r.path, 'okc'); VCS.toast('已保存预览'); }
    else VCS.log('保存失败:' + ((r && r.error) || '未知'), 'failc');
  }

  // ── 浏览:pick_file(文件)/ pick_dir(目录)→ 回填输入;取消(path=null)不改值、不崩 ──
  async function pickFile(id, kind) {
    const r = await VCS.call('pick_file', kind);
    if (r && r.error) { VCS.log('选择文件失败:' + r.error, 'failc'); return; }
    if (r && r.path) { setVal(id, r.path); refreshPreview(); }
  }
  async function pickDir(id) {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log('选择目录失败:' + r.error, 'failc'); return; }
    if (r && r.path) setVal(id, r.path);
  }

  // ── 一键生成:gen_run → 成功落台账 + 逐条 warnings + 提示去任务页;失败 log 错误 ──
  async function run() {
    const btn = $('gen-run');
    const poscar = val('gen-poscar'), incar = val('gen-incar');
    const out = val('gen-out'), lib = val('gen-lib');
    const calc = val('gen-calc') || 'slab';
    const extraKw = val('gen-extra-kw');
    if (btn) btn.disabled = true;
    VCS.log('生成中(' + calc + ')…');
    try {
      const r = await VCS.call('gen_run', poscar, incar, out, lib, calc, extraKw);
      if (!r || r.ok === false || r.error) {
        VCS.log('生成失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      VCS.log('已生成:' + r.job_dir, 'okc');
      (r.warnings || []).forEach(w => VCS.log(w));
      VCS.log('作业已入台账,去任务页提交', 'okc');
      VCS.call('open_dir', r.job_dir);          // 输出反馈统一:打开四件套所在目录
      VCS.toast('已生成四件套');
      // 同步任务页台账(若已加载)
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── SAC 批量建模:chips 多选 + 预览矩阵 + 生成矩阵(生成前弹确认显示预估) ──
  const SAC_METALS = ['Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Mo', 'W'];
  const SAC_TEMPLATES = ['MN4', 'MN3', 'MP1N3', 'MS1N3', 'MB1N3', 'MN4+B'];

  function renderChips(id, items, preselect) {
    const box = $(id);
    if (!box) return;
    box.innerHTML = '';
    items.forEach(it => {
      const c = document.createElement('span');
      c.className = 'chip' + (preselect && preselect.indexOf(it) >= 0 ? ' on' : '');
      c.textContent = it;
      c.dataset.val = it;
      c.addEventListener('click', () => c.classList.toggle('on'));
      box.appendChild(c);
    });
  }
  function chipVals(id) {
    const box = $(id);
    return box ? Array.from(box.querySelectorAll('.chip.on')).map(c => c.dataset.val) : [];
  }
  function sacMetals() {
    const base = chipVals('sac-metals');
    const extra = (val('sac-metal-more') || '').split(',').map(s => s.trim()).filter(Boolean);
    return Array.from(new Set(base.concat(extra)));
  }
  function sacParams() {
    return {
      metals: sacMetals(), templates: chipVals('sac-templates'), ads: chipVals('sac-ads'),
      sites: val('sac-sites') || 'metal_top', rot: parseInt(val('sac-rot') || '1', 10) || 1,
    };
  }

  async function loadSacAdsorbates() {
    const r = await VCS.call('molecule_list');
    const mols = (r && r.molecules) || [];
    renderChips('sac-ads', mols.map(m => m.name), []);
  }

  async function sacPreview() {
    const p = sacParams();
    const box = $('sac-preview');
    if (!p.metals.length || !p.templates.length) {
      if (box) box.textContent = '请至少选择一个金属与一个模板';
      return null;
    }
    if (box) box.textContent = '估算中…';
    const r = await VCS.call('sac_matrix_preview', p.metals, p.templates, p.ads, p.sites, p.rot);
    if (!r || r.ok === false || r.error) {
      if (box) box.textContent = '预览失败:' + ((r && r.error) || '未知错误');
      return null;
    }
    if (box) {
      const eg = (r.names || []).slice(0, 6).join('、');
      box.innerHTML = `矩阵规模:<b>${r.n_slabs}</b> 清洁面 + <b>${r.n_configs}</b> 吸附构型 = ` +
        `<b>${r.n_total_jobs}</b> 个作业<br>${VCS.esc(r.estimate_note)}` +
        (eg ? `<br>示例:${VCS.esc(eg)} …` : '');
    }
    return r;
  }

  async function sacGenerate() {
    const p = sacParams();
    const incar = val('sac-incar'), out = val('sac-out');
    if (!p.metals.length || !p.templates.length) {
      VCS.log('SAC:请至少选择一个金属与一个模板', 'failc'); return;
    }
    if (!incar) { VCS.log('SAC:请选择共享 INCAR', 'failc'); return; }
    if (!out) { VCS.log('SAC:请选择输出根目录', 'failc'); return; }
    const pv = await sacPreview();     // 生成前弹确认显示预估(规模 + 粗估机时)
    const msg = pv
      ? `将生成约 ${pv.n_total_jobs} 个作业(${pv.n_slabs} 清洁面 + ${pv.n_configs} 构型)。\n` +
        `${pv.estimate_note}\n\n继续?`
      : `将生成 SAC 候选矩阵(${p.metals.length} 金属 × ${p.templates.length} 模板)。继续?`;
    if (!await VCS.confirm(msg)) return;
    const btn = $('sac-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log('SAC 批量建模生成中…');
    try {
      const r = await VCS.call('sac_matrix_generate', p.metals, p.templates, p.ads,
        p.sites, p.rot, incar, out);
      if (!r || r.ok === false || r.error) {
        VCS.log('SAC 生成失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
      }
      VCS.log('SAC 已生成 ' + r.created + ' 个作业,入台账', 'okc');
      (r.project_paths || []).forEach(pp => VCS.log('已建吸附能项目:' + pp, 'okc'));
      (r.skipped || []).forEach(s => VCS.log('跳过 ' + (s.name || '') + ':' + s.reason, 'warnc'));
      if (r.campaign) VCS.log('已注册批次(campaign):' + r.campaign, 'okc');
      VCS.toast('SAC 已生成 ' + r.created + ' 个作业');
      VCS.call('open_dir', out);
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 多自旋并跑:生成 NM/LS/HS 家族 ────────────────────────────────────────
  async function spinGenerate() {
    const pos = val('spin-poscar'), incar = val('spin-incar'), out = val('spin-out');
    if (!pos) { VCS.log('自旋:请选择结构 POSCAR', 'failc'); return; }
    if (!incar) { VCS.log('自旋:请选择 INCAR', 'failc'); return; }
    if (!out) { VCS.log('自旋:请选择输出根目录', 'failc'); return; }
    const btn = $('spin-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log('多自旋家族生成中…');
    try {
      const r = await VCS.call('spin_family_generate', pos, incar, out);
      if (!r || r.ok === false || r.error) {
        VCS.log('自旋家族生成失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
      }
      (r.variants || []).forEach(v => {
        VCS.log('已生成自旋变体 ' + v.name + ':' + v.job_dir +
          (v.magmom ? '(MAGMOM ' + v.magmom + ')' : ''), 'okc');
        (v.warnings || []).forEach(w => VCS.log('  ⚠ ' + w, 'warnc'));
      });
      VCS.log('自旋家族已入台账;全部 DONE 后在作业页对该家族「自旋对比」判基态', 'okc');
      VCS.toast('已生成 ' + (r.variants || []).length + ' 个自旋变体');
      VCS.call('open_dir', out);
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 引擎选择器:VASP(用上方四件套)/ CP2K / Gaussian / CASTEP(文件级适配,简化表单) ──
  const State2 = { engine: 'vasp' };
  async function loadEngines() {
    const box = $('engine-chips');
    if (!box) return;
    const sceneKey = (window.VCS && VCS.scenario && VCS.scenario.key) || null;
    const r = await VCS.call('engine_list', sceneKey);
    const engines = (r && r.engines) || [];
    box.innerHTML = '';
    engines.forEach(e => {
      const c = document.createElement('span');
      c.className = 'chip' + (e.key === State2.engine ? ' on' : '');
      c.dataset.val = e.key;
      c.textContent = e.name + (e.experimental ? ' (实验性)' : '');
      c.addEventListener('click', () => selectEngine(e.key));
      box.appendChild(c);
    });
  }
  async function selectEngine(key) {
    State2.engine = key;
    document.querySelectorAll('#engine-chips .chip').forEach(
      c => c.classList.toggle('on', c.dataset.val === key));
    const isGauss = (key === 'gaussian');
    // 通用简化表单:cp2k/castep 用;vasp 走上方四件套;gaussian 走专属分子面板
    const form = $('engine-form');
    if (form) form.hidden = (key === 'vasp' || isGauss);
    const gp = $('gauss-panel');
    if (gp) gp.hidden = !isGauss;
    if (isGauss && window.GaussMol && window.GaussMol.onShow) window.GaussMol.onShow();
    const banner = $('engine-nonequiv');
    if (banner) {
      if (key === 'vasp') { banner.hidden = true; }
      else {
        const nr = await VCS.call('engine_nonequiv', 'vasp', key);
        const rep = (nr && nr.report) || [];
        banner.hidden = !rep.length;
        banner.innerHTML = '<b>跨引擎不等价(需逐项人工确认):</b><br>' +
          rep.map(x => VCS.esc(x)).join('<br>');
      }
    }
  }
  async function engineGenerate() {
    const eng = State2.engine;
    const out = val('eng-out');
    if (!val('eng-poscar')) { VCS.log('引擎:请选择结构 POSCAR', 'failc'); return; }
    if (!out) { VCS.log('引擎:请选择输出目录', 'failc'); return; }
    const kpts = val('eng-kpts').split(/[\s,]+/).map(s => parseInt(s, 10)).filter(n => !isNaN(n));
    const params = {
      poscar: val('eng-poscar'), functional: val('eng-func') || 'PBE',
      dispersion: val('eng-disp') || null,
      cutoff_ev: val('eng-cutoff') || null,
      kpoints: kpts.length >= 3 ? kpts.slice(0, 3) : null,
      periodic: (val('eng-periodic') || '1') === '1',
      spin: $('eng-spin') ? $('eng-spin').checked : false,
      charge: parseInt(val('eng-charge') || '0', 10) || 0,
    };
    const btn = $('engine-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log('生成 ' + eng + ' 引擎输入(文件级适配)…');
    try {
      const r = await VCS.call('engine_generate', eng, params, out);
      if (!r || r.ok === false || r.error) {
        VCS.log('引擎生成失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
      }
      (r.files || []).forEach(f => VCS.log('已生成:' + f, 'okc'));
      (r.issues || []).forEach(i => VCS.log('自洽校验:' + i, 'warnc'));
      (r.warnings || []).forEach(w => VCS.log(w, 'warnc'));
      VCS.log(eng + ' 引擎输入已生成(软件本体用户自备)', 'okc');
      VCS.call('open_dir', out);
      VCS.toast('已生成 ' + eng + ' 输入');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 计算活动模板(SAC 卡):选模板 → instantiate 全链 DAG ──
  const CampState = { templates: [] };
  async function loadCampaignTemplates() {
    const sel = $('camp-tpl-sel');
    if (!sel) return;
    const r = await VCS.call('campaign_templates');
    CampState.templates = (r && r.templates) || [];
    sel.innerHTML = '<option value="">不使用模板(仅建吸附能项目矩阵)</option>' +
      CampState.templates.map(t => `<option value="${VCS.esc(t.key)}">${VCS.esc(t.name_zh)}</option>`).join('');
  }
  function onCampTplChange() {
    const sel = $('camp-tpl-sel');
    const desc = $('camp-tpl-desc');
    if (!sel || !desc) return;
    const t = CampState.templates.find(x => x.key === sel.value);
    if (!t) { desc.hidden = true; return; }
    desc.hidden = false;
    desc.innerHTML = `<b>${VCS.esc(t.name_zh)}</b>:${VCS.esc(t.description)}<br>` +
      `阶段数 ${t.n_stages} · 汇总 ${(t.analyses || []).join('、')} · 图场景 ${VCS.esc(t.figures_scenario || '—')}`;
  }
  async function instantiateCampaign() {
    const sel = $('camp-tpl-sel');
    const key = sel ? sel.value : '';
    if (!key) { VCS.log('请先选计算活动模板', 'failc'); return; }
    const p = sacParams();
    const out = val('sac-out');
    if (!p.metals.length || !p.templates.length) { VCS.log('请先在下方选金属与配位模板', 'failc'); return; }
    if (!out) { VCS.log('请选输出根目录', 'failc'); return; }
    const systems = [];
    p.metals.forEach(m => p.templates.forEach(t => systems.push(m + '@' + t)));
    const spec = { systems: systems, adsorbates: p.ads, clean: true };
    if (!await VCS.confirm('将按模板「' + sel.options[sel.selectedIndex].text + '」生成 ' +
      systems.length + ' 体系的全链任务 DAG(relax→静态→频率→汇总)。继续?')) return;
    VCS.log('按模板实例化全链 DAG…');
    const r = await VCS.call('campaign_instantiate', key, spec, out, null);
    if (!r || r.ok === false || r.error) { VCS.log('实例化失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    const est = (r.estimate && r.estimate.total) || 0;
    VCS.log('已生成全链 DAG:' + r.n_jobs + ' 个作业,约 ' + est + ' 核时 → ' + r.campaign_dir, 'okc');
    VCS.toast('已按模板生成全链批次');
  }

  // ── 初始化:回填上次路径 + 首帧预览,绑定浏览/生成/输入监听 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }

  async function init() {
    wire('gen-poscar-btn', () => pickFile('gen-poscar', 'poscar'));
    wire('gen-poscar-3d', () => {
      const p = val('gen-poscar');
      if (!p) { VCS.log('请先选择 POSCAR 文件', 'failc'); return; }
      VCS.showStructure(p, null, p.replace(/[\\/]+$/, '').split(/[\\/]/).pop());
    });
    wire('gen-incar-btn', () => pickFile('gen-incar', 'incar'));
    wire('gen-out-btn', () => pickDir('gen-out'));
    wire('gen-lib-btn', () => pickDir('gen-lib'));
    wire('gen-run', run);
    // POSCAR/INCAR 手动改路径(change/blur)也触发预览
    ['gen-poscar', 'gen-incar'].forEach(id => {
      const el = $(id);
      if (el) { el.addEventListener('change', refreshPreview); el.addEventListener('blur', refreshPreview); }
    });
    // 切换计算类型即刷新预览(KPOINTS 随之变化)
    { const el = $('gen-calc'); if (el) el.addEventListener('change', refreshPreview); }

    // SAC 批量建模:chips 预置 + 吸附质从分子库载入 + 浏览/预览/生成
    renderChips('sac-metals', SAC_METALS, ['Fe']);
    renderChips('sac-templates', SAC_TEMPLATES, ['MN4']);
    loadSacAdsorbates();
    wire('sac-incar-btn', () => pickFile('sac-incar', 'incar'));
    wire('sac-out-btn', () => pickDir('sac-out'));
    wire('sac-preview-btn', sacPreview);
    wire('sac-gen-btn', sacGenerate);
    // 计算活动模板 + 预览保存
    loadCampaignTemplates();
    { const el = $('camp-tpl-sel'); if (el) el.addEventListener('change', onCampTplChange); }
    wire('camp-tpl-inst', instantiateCampaign);
    wire('gen-preview-save', savePreview);
    // 多自旋并跑:浏览/生成
    wire('spin-poscar-btn', () => pickFile('spin-poscar', 'poscar'));
    wire('spin-incar-btn', () => pickFile('spin-incar', 'incar'));
    wire('spin-out-btn', () => pickDir('spin-out'));
    wire('spin-gen-btn', spinGenerate);

    // 引擎选择器(生成页):载入引擎 chips + 浏览/生成
    loadEngines();
    wire('eng-poscar-btn', () => pickFile('eng-poscar', 'poscar'));
    wire('eng-out-btn', () => pickDir('eng-out'));
    wire('engine-gen-btn', engineGenerate);

    const st = await VCS.call('gen_state');
    if (st) {
      setVal('gen-poscar', st.poscar);
      setVal('gen-incar', st.incar);
      setVal('gen-out', st.out_dir);
      setVal('gen-lib', st.lib_root);
      if (st.error) VCS.log('读取上次路径失败:' + st.error, 'failc');
    }
    refreshPreview();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // 供 molbuild.js(①分子建模「下一步」)携分子进 Gaussian 面板:选 Gaussian 引擎 + 载分子
  async function useMolecule(struct) {
    await loadEngines();
    selectEngine('gaussian');
    if (window.GaussMol && typeof window.GaussMol.useMolecule === 'function') {
      window.GaussMol.useMolecule(struct);
    }
  }

  window.Generate = { reload: () => refreshPreview(), useMolecule };
})();
