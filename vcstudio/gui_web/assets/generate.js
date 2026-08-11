// generate.js — 生成页:POSCAR/INCAR/输出目录/赝势库四行(文本框+浏览)+ 即时解析预览
// + 「生成四件套」一键生成。行为对齐 vcstudio/gui/generate_tab.py:
// 启动 gen_state 回填最近路径;POSCAR+INCAR 齐备即调 gen_preview 渲染到 <pre class="mono">;
// gen_run 成功后逐条 log warnings + 提示去任务页,并刷新任务页台账。
// 只依赖 app.js 暴露的 VCS.*;所有插值走 VCS.esc(pre 用 textContent 天然安全);零 emoji;中文文案。
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
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = v || ''; };
  const State = {
    previewKey: null,
    previewView: { kind: 'waiting', error: '' },
    solvationPreview: null,
  };   // 预览去重 + 防旧响应覆盖

  function renderPreviewLanguage() {
    const pre = $('gen-preview');
    if (!pre || State.previewView.kind === 'content') return;
    const setTxt = t => { if ('value' in pre) pre.value = t; else pre.textContent = t; };
    pre.style.color = State.previewView.kind === 'failed' ? 'var(--fail)' : '';
    if (State.previewView.kind === 'parsing') {
      setTxt(tr('runtime.generate.preview.parsing', {}, '正在解析…', 'Parsing…'));
    } else if (State.previewView.kind === 'failed') {
      setTxt(tr('runtime.generate.preview.failed', {
        error: State.previewView.error || tr(
          'runtime.generate.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '预览失败:{error}', 'Preview failed: {error}'));
    } else if (State.previewView.kind === 'empty') {
      setTxt(tr('runtime.generate.preview.empty_content', {},
        '(无预览内容)', '(No preview content)'));
    } else {
      setTxt(tr('runtime.generate.preview.waiting', {},
        '(选择 POSCAR / INCAR 后自动解析预览)',
        '(Select POSCAR and INCAR to parse the preview automatically)'));
    }
  }

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
      State.previewView = { kind: 'waiting', error: '' };
      renderPreviewLanguage();
      return;
    }
    const key = poscar + '\n' + incar + '\n' + calc;
    if (key === State.previewKey) return;   // 相同输入不重复解析(镜像 _preview_memo)
    State.previewKey = key;
    State.previewView = { kind: 'parsing', error: '' };
    renderPreviewLanguage();
    const r = await VCS.call('gen_preview', poscar, incar, calc);
    if (State.previewKey !== key) return;   // 期间用户又改了路径 → 丢弃旧响应
    if (!r || r.ok === false || r.error) {
      State.previewView = { kind: 'failed', error: (r && r.error) || '' };
      renderPreviewLanguage();
      return;
    }
    const s = r.summary || {};
    pre.style.color = '';
    const content = [s.poscar, s.incar].filter(Boolean).join('\n\n');
    State.previewView = { kind: content ? 'content' : 'empty', error: '' };
    if (content) setTxt(content);
    else renderPreviewLanguage();
  }

  // 保存预览到文件(可编辑预览区内容 → save_text)
  async function savePreview() {
    const pre = $('gen-preview');
    const txt = pre ? ('value' in pre ? pre.value : pre.textContent) : '';
    if (!txt || !txt.trim()) {
      VCS.log(tr('runtime.generate.preview.nothing_to_save', {},
        '预览为空,无内容可保存', 'The preview is empty; there is nothing to save'), 'failc');
      return;
    }
    const d = await VCS.call('pick_dir');
    if (!d || !d.path) return;
    const sep = d.path.indexOf('\\') >= 0 ? '\\' : '/';
    const dest = d.path + sep + 'preview.txt';
    const r = await VCS.call('save_text', dest, txt);
    if (r && r.ok) {
      VCS.log(tr('runtime.generate.preview.saved_path', { path: r.path },
        '预览已保存:{path}', 'Preview saved: {path}'), 'okc');
      VCS.toast(tr('runtime.generate.preview.saved', {}, '已保存预览', 'Preview saved'));
    } else VCS.log(tr('runtime.generate.preview.save_failed', {
      error: (r && r.error) || tr('runtime.generate.common.unknown', {}, '未知', 'Unknown'),
    }, '保存失败:{error}', 'Save failed: {error}'), 'failc');
  }

  // ── 浏览:pick_file(文件)/ pick_dir(目录)→ 回填输入;取消(path=null)不改值、不崩 ──
  async function pickFile(id, kind) {
    const r = await VCS.call('pick_file', kind);
    if (r && r.error) {
      VCS.log(tr('runtime.generate.picker.file_failed', { error: r.error },
        '选择文件失败:{error}', 'Failed to select file: {error}'), 'failc');
      return;
    }
    if (r && r.path) { setVal(id, r.path); refreshPreview(); }
  }
  async function pickDir(id) {
    const r = await VCS.call('pick_dir');
    if (r && r.error) {
      VCS.log(tr('runtime.generate.picker.directory_failed', { error: r.error },
        '选择目录失败:{error}', 'Failed to select directory: {error}'), 'failc');
      return;
    }
    if (r && r.path) setVal(id, r.path);
  }

  // ── 一键生成:gen_run → 成功落台账 + 逐条 warnings + 提示去任务页;失败 log 错误 ──
  // 隐式溶剂化(VASPsol)勾选状态 → gen_run 的 solvation 参数(默认关)
  function solvationParam() {
    const on = $('gen-solvation') && $('gen-solvation').checked;
    if (!on) return null;
    const ebk = parseFloat(val('gen-solvation-ebk') || '78.4');
    return { enabled: true, eb_k: isNaN(ebk) ? 78.4 : ebk };
  }
  // 勾选/改介电常数时预览将写入的键 + 补丁编译 warning(诚实提醒真空陷阱)
  function renderSolvationNote() {
    const note = $('gen-solvation-note');
    if (!note) return;
    const on = $('gen-solvation') && $('gen-solvation').checked;
    if (!on) { note.hidden = true; note.textContent = ''; return; }
    const r = State.solvationPreview;
    if (!r || r.ok === false) { note.hidden = true; note.textContent = ''; return; }
    note.hidden = false;
    note.textContent = tr('runtime.generate.solvation.append_note', {
      lines: (r.incar_lines || []).join(' · '), warning: r.warning || '',
    }, '将追加:{lines}。{warning}', 'Will append: {lines}. {warning}');
  }
  async function refreshSolvationNote() {
    const note = $('gen-solvation-note');
    if (!note) return;
    const on = $('gen-solvation') && $('gen-solvation').checked;
    if (!on) {
      State.solvationPreview = null;
      renderSolvationNote();
      return;
    }
    const ebk = parseFloat(val('gen-solvation-ebk') || '78.4');
    const r = await VCS.call('vaspsol_preview', isNaN(ebk) ? 78.4 : ebk, true);
    State.solvationPreview = r || null;
    renderSolvationNote();
  }

  async function run() {
    const btn = $('gen-run');
    const poscar = val('gen-poscar'), incar = val('gen-incar');
    const out = val('gen-out'), lib = val('gen-lib');
    const calc = val('gen-calc') || 'slab';
    const extraKw = val('gen-extra-kw');
    const solvation = solvationParam();
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.generate.run.running', {
      calculation: calc, solvation: solvation ? ', VASPsol' : '',
    }, '生成中({calculation}{solvation})…', 'Generating ({calculation}{solvation})…'));
    try {
      const r = await VCS.call('gen_run', poscar, incar, out, lib, calc, extraKw, solvation);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.generate.run.failed', {
          error: (r && r.error) || tr(
            'runtime.generate.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '生成失败:{error}', 'Generation failed: {error}'), 'failc');
        return;
      }
      VCS.log(tr('runtime.generate.run.created_path', { path: r.job_dir },
        '已生成:{path}', 'Generated: {path}'), 'okc');
      (r.warnings || []).forEach(w => VCS.log(w));
      VCS.log(tr('runtime.generate.run.registered', {},
        '作业已入台账,去任务页提交',
        'The job was added to the ledger; submit it from the Jobs page'), 'okc');
      VCS.call('open_dir', r.job_dir);          // 输出反馈统一:打开四件套所在目录
      VCS.toast(tr('runtime.generate.run.four_files_created', {},
        '已生成四件套', 'Four-file input set generated'));
      // 同步任务页台账(若已加载)
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
      // 生成是流程中点,不让新手只在日志里猜后续步骤。主按钮走 app.js
      // 统一导航,进任务页后自动选中刚生成的 CREATED 作业。
      if (typeof VCS.nextStep === 'function') {
        VCS.nextStep({
          title: tr('runtime.generate.next_step.title', {},
            '四件套已生成', 'Four-file input set generated'),
          message: tr('runtime.generate.next_step.message', {},
            '作业已加入任务列表。', 'The job was added to the job list.'),
          detail: tr('runtime.generate.next_step.detail', {},
            '下一步：前往任务页，确认目标集群，然后点击“上传并提交”。',
            'Next: open Jobs, confirm the target cluster, then select Upload and submit.'),
          primaryLabel: tr('runtime.generate.next_step.primary', {},
            '前往任务页并提交', 'Open Jobs and submit'),
          page: 'jobs',
          focusJobDir: r.job_dir,
        });
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── SAC 批量建模:chips 多选 + 预览矩阵 + 生成矩阵(生成前弹确认显示预估) ──
  const SAC_METALS = ['Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Mo', 'W'];
  const SAC_TEMPLATES = ['MN4', 'MN3', 'MP1N3', 'MS1N3', 'MB1N3', 'MN4+B'];

  // chips 墙 → 多选下拉(保留原容器 id,只改内部渲染;被 api 消费的选中值数组契约不变)
  function renderChips(id, items, preselect) {
    const box = $(id);
    if (!box) return;
    if (window.VCS && VCS.ui && VCS.ui.multiselect) {
      VCS.ui.multiselect(box, {
        items: items.map(v => ({ val: v, label: v })),
        selected: (preselect || []).filter(v => items.indexOf(v) >= 0),
        placeholder: tr('runtime.generate.multiselect.placeholder', {},
          '点此选择(可多选)', 'Select one or more'),
      });
    } else {                                   // 兜底:ui.js 缺失时退回 chips
      box.innerHTML = '';
      items.forEach(it => {
        const c = document.createElement('span');
        c.className = 'chip' + (preselect && preselect.indexOf(it) >= 0 ? ' on' : '');
        c.textContent = it; c.dataset.val = it;
        c.addEventListener('click', () => c.classList.toggle('on'));
        box.appendChild(c);
      });
    }
  }
  function chipVals(id) {
    const box = $(id);
    if (!box) return [];
    if (box._ms) return box._ms.getSelected();
    return Array.from(box.querySelectorAll('.chip.on')).map(c => c.dataset.val);
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
      if (box) box.textContent = tr('runtime.generate.sac.selection_required', {},
        '请至少选择一个金属与一个模板',
        'Select at least one metal and one template');
      return null;
    }
    if (box) box.textContent = tr('runtime.generate.sac.estimating', {},
      '估算中…', 'Estimating…');
    const r = await VCS.call('sac_matrix_preview', p.metals, p.templates, p.ads, p.sites, p.rot);
    if (!r || r.ok === false || r.error) {
      if (box) box.textContent = tr('runtime.generate.sac.preview_failed', {
        error: (r && r.error) || tr(
          'runtime.generate.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '预览失败:{error}', 'Preview failed: {error}');
      return null;
    }
    if (box) {
      const eg = (r.names || []).slice(0, 6).join(', ');
      box.innerHTML = VCS.esc(tr('runtime.generate.sac.preview_summary', {
        slabs: r.n_slabs, configurations: r.n_configs, jobs: r.n_total_jobs,
      }, '矩阵规模:{slabs} 清洁面 + {configurations} 吸附构型 = {jobs} 个作业',
      'Matrix size: {slabs} clean surfaces + {configurations} adsorption configurations = {jobs} jobs')) +
        `<br>${VCS.esc(r.estimate_note)}` +
        (eg ? `<br>${VCS.esc(tr('runtime.generate.sac.examples', { examples: eg },
          '示例:{examples} …', 'Examples: {examples} …'))}` : '');
    }
    return r;
  }

  async function sacGenerate() {
    const p = sacParams();
    const incar = val('sac-incar'), out = val('sac-out');
    if (!p.metals.length || !p.templates.length) {
      VCS.log(tr('runtime.generate.sac.log_selection_required', {},
        'SAC:请至少选择一个金属与一个模板',
        'SAC: select at least one metal and one template'), 'failc'); return;
    }
    if (!incar) {
      VCS.log(tr('runtime.generate.sac.incar_required', {},
        'SAC:请选择共享 INCAR', 'SAC: select a shared INCAR'), 'failc'); return;
    }
    if (!out) {
      VCS.log(tr('runtime.generate.sac.output_required', {},
        'SAC:请选择输出根目录', 'SAC: select an output root'), 'failc'); return;
    }
    const pv = await sacPreview();     // 生成前弹确认显示预估(规模 + 粗估机时)
    const msg = pv
      ? tr('runtime.generate.sac.confirm_estimate', {
        jobs: pv.n_total_jobs, slabs: pv.n_slabs, configurations: pv.n_configs,
        estimate: pv.estimate_note,
      }, '将生成约 {jobs} 个作业({slabs} 清洁面 + {configurations} 构型)。\n{estimate}\n\n继续?',
      'Generate approximately {jobs} jobs ({slabs} clean surfaces + {configurations} configurations)?\n{estimate}\n\nContinue?')
      : tr('runtime.generate.sac.confirm_matrix', {
        metals: p.metals.length, templates: p.templates.length,
      }, '将生成 SAC 候选矩阵({metals} 金属 × {templates} 模板)。继续?',
      'Generate an SAC candidate matrix ({metals} metals × {templates} templates)?');
    if (!await VCS.confirm(msg)) return;
    const btn = $('sac-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.generate.sac.generating', {},
      'SAC 批量建模生成中…', 'Generating SAC batch models…'));
    try {
      const r = await VCS.call('sac_matrix_generate', p.metals, p.templates, p.ads,
        p.sites, p.rot, incar, out);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.generate.sac.generate_failed', {
          error: (r && r.error) || tr(
            'runtime.generate.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, 'SAC 生成失败:{error}', 'SAC generation failed: {error}'), 'failc'); return;
      }
      VCS.log(tr('runtime.generate.sac.created', { count: r.created },
        'SAC 已生成 {count} 个作业,入台账',
        'SAC generated and registered {count} jobs'), 'okc');
      (r.projects || []).forEach(project => VCS.log(tr(
        'runtime.generate.sac.project_created', {
          path: project.name || project.project_id || '',
        }, '已建吸附能项目:{path}', 'Adsorption-energy project created: {path}'), 'okc'));
      (r.skipped || []).forEach(s => VCS.log(tr(
        'runtime.generate.sac.skipped', { name: s.name || '', reason: s.reason },
        '跳过 {name}:{reason}', 'Skipped {name}: {reason}'), 'warnc'));
      if (r.campaign) VCS.log(tr('runtime.generate.sac.campaign_registered', {
        campaign: r.campaign,
      }, '已注册批次(campaign):{campaign}', 'Campaign registered: {campaign}'), 'okc');
      VCS.toast(tr('runtime.generate.sac.toast_created', { count: r.created },
        'SAC 已生成 {count} 个作业', 'SAC generated {count} jobs'));
      VCS.call('open_dir', out);
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 金属 slab 建模:fcc/bcc/hcp 低指数面(层厚收敛的可再生入口,Backlog #2) ──
  const MSlab = { surfaces: [], guess: {} };
  async function loadMetalSlabCatalog() {
    const sel = $('mslab-surface');
    if (!sel) return;
    const r = await VCS.call('metal_slab_catalog');
    if (!r || r.ok === false) {
      sel.innerHTML = `<option value="">${VCS.esc(tr(
        'runtime.generate.metal_slab.catalog_failed', {},
        '(晶面目录加载失败)', '(Failed to load surface catalog)',
      ))}</option>`;
      return;
    }
    MSlab.surfaces = r.surfaces || [];
    MSlab.guess = r.guess || {};
    sel.innerHTML = MSlab.surfaces.map(s =>
      `<option value="${VCS.esc(s.structure + ':' + s.miller)}">` +
      `${VCS.esc(s.structure + '(' + s.miller + ')')}</option>`).join('');
    onMslabChange();
  }
  function mslabSel() {
    const v = (val('mslab-surface') || 'fcc:111').split(':');
    return { structure: v[0] || 'fcc', miller: v[1] || '111' };
  }
  // 元素/晶面联动:hcp 显示 c 输入;提示晶格常数初猜(实验值,发表口径须 EOS/晶胞优化)
  function onMslabChange() {
    const s = mslabSel().structure;
    const cIpt = $('mslab-c');
    if (cIpt) cIpt.hidden = s !== 'hcp';
    const tip = $('mslab-guess');
    if (!tip) return;
    const el = val('mslab-el');
    if (!el) { tip.textContent = ''; return; }
    const g = (MSlab.guess[s] || {})[el];
    if (g === undefined) {
      tip.textContent = tr('runtime.generate.metal_slab.guess_missing', {
        element: el, structure: s,
      }, '{element} 不在 {structure} 初猜表:请显式填晶格常数(建议来自同泛函 EOS/晶胞优化)。',
      '{element} is not in the {structure} initial-guess table; enter the lattice constant explicitly, preferably from same-functional EOS or cell optimization.');
    } else if (typeof g === 'object') {
      tip.textContent = tr('runtime.generate.metal_slab.guess_hcp', { a: g.a, c: g.c },
        '初猜:a={a} Å、c={c} Å(实验值;发表口径须晶胞优化定终值)。',
        'Initial guess: a={a} Å, c={c} Å (experimental values; publication values require cell optimization).');
    } else {
      tip.textContent = tr('runtime.generate.metal_slab.guess', { a: g },
        '初猜:a={a} Å(实验值;发表口径须 EOS/晶胞优化定终值)。',
        'Initial guess: a={a} Å (experimental value; publication values require EOS or cell optimization).');
    }
  }
  async function mslabGenerate() {
    const el = val('mslab-el'), out = val('mslab-out');
    if (!el) {
      VCS.log(tr('runtime.generate.metal_slab.element_required', {},
        '金属 slab:请填元素符号', 'Metal slab: enter an element symbol'), 'failc'); return;
    }
    if (!out) {
      VCS.log(tr('runtime.generate.metal_slab.output_required', {},
        '金属 slab:请选择输出作业目录', 'Metal slab: select an output job directory'), 'failc'); return;
    }
    const s = mslabSel();
    const btn = $('mslab-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.generate.metal_slab.generating', {
      element: el, structure: s.structure, miller: s.miller,
    }, '金属 slab 建模:{element} {structure}({miller})…',
    'Building metal slab: {element} {structure}({miller})…'));
    try {
      const r = await VCS.call('metal_slab_build', el, s.structure, s.miller,
        parseInt(val('mslab-layers') || '4', 10) || 4,
        val('mslab-a') || null, val('mslab-c') || null,
        parseInt(val('mslab-nx') || '3', 10) || 3,
        parseInt(val('mslab-ny') || '3', 10) || 3,
        parseFloat(val('mslab-vac') || '15') || 15,
        parseInt(val('mslab-fix') || '0', 10) || 0,
        val('mslab-incar') || null, out);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.generate.metal_slab.failed', {
          error: (r && r.error) || tr(
            'runtime.generate.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '金属 slab 生成失败:{error}', 'Metal-slab generation failed: {error}'), 'failc');
        return;
      }
      VCS.log(tr('runtime.generate.metal_slab.created', { description: r.description },
        '已生成:{description}', 'Generated: {description}'), 'okc');
      (r.warnings || []).forEach(w => VCS.log('⚠ ' + w, 'warnc'));
      VCS.log(tr('runtime.generate.metal_slab.job_directory', { path: r.job_dir },
        '作业目录:{path}(job.yaml 带可再生配方;层厚收敛可从此作业一键派生)',
        'Job directory: {path} (job.yaml contains a reproducible recipe; layer-thickness convergence can be derived from this job)'), 'okc');
      VCS.toast(tr('runtime.generate.metal_slab.complete', {},
        '金属 slab 已生成', 'Metal slab generated'));
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 多自旋并跑:生成 NM/LS/HS 家族 ────────────────────────────────────────
  async function spinGenerate() {
    const pos = val('spin-poscar'), incar = val('spin-incar'), out = val('spin-out');
    if (!pos) {
      VCS.log(tr('runtime.generate.spin.poscar_required', {},
        '自旋:请选择结构 POSCAR', 'Spin: select a structure POSCAR'), 'failc'); return;
    }
    if (!incar) {
      VCS.log(tr('runtime.generate.spin.incar_required', {},
        '自旋:请选择 INCAR', 'Spin: select an INCAR'), 'failc'); return;
    }
    if (!out) {
      VCS.log(tr('runtime.generate.spin.output_required', {},
        '自旋:请选择输出根目录', 'Spin: select an output root'), 'failc'); return;
    }
    const btn = $('spin-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.generate.spin.generating', {},
      '多自旋家族生成中…', 'Generating the multi-spin family…'));
    try {
      const r = await VCS.call('spin_family_generate', pos, incar, out);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.generate.spin.failed', {
          error: (r && r.error) || tr(
            'runtime.generate.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '自旋家族生成失败:{error}', 'Spin-family generation failed: {error}'), 'failc'); return;
      }
      (r.variants || []).forEach(v => {
        VCS.log(tr('runtime.generate.spin.variant_created', {
          name: v.name, path: v.job_dir, magmom: v.magmom ? `(MAGMOM ${v.magmom})` : '',
        }, '已生成自旋变体 {name}:{path}{magmom}',
        'Generated spin variant {name}: {path}{magmom}'), 'okc');
        (v.warnings || []).forEach(w => VCS.log('  ⚠ ' + w, 'warnc'));
      });
      VCS.log(tr('runtime.generate.spin.registered', {},
        '自旋家族已入台账;全部 DONE 后在作业页对该家族「自旋对比」判基态',
        'The spin family was registered; after every member is DONE, use Spin comparison on the Jobs page to determine the ground state'), 'okc');
      VCS.toast(tr('runtime.generate.spin.complete', { count: (r.variants || []).length },
        '已生成 {count} 个自旋变体', 'Generated {count} spin variants'));
      VCS.call('open_dir', out);
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 引擎选择器:VASP(用上方四件套)/ CP2K / Gaussian / CASTEP(文件级适配,简化表单) ──
  const State2 = { engine: 'vasp', sceneKey: null, engines: [], capabilities: {} };
  // 引擎选择器:VASP 置顶(主引擎)的下拉,替代原 chips 墙。默认 VASP。
  async function loadEngines() {
    const sel = $('engine-select');
    if (!sel) return;
    const sceneKey = (window.VCS && VCS.scenario && VCS.scenario.key) || null;
    const [r, current] = await Promise.all([
      VCS.call('engine_list', sceneKey), VCS.call('engine_get')]);
    let engines = ((r && r.engines) || []).filter(e => e.visible !== false);
    if (!engines.length) engines = [{ key: 'vasp', name: 'VASP', experimental: false }];
    // VASP 恒置顶醒目;其余引擎(实验性)靠后
    engines = engines.slice().sort((a, b) => (a.key === 'vasp' ? -1 : b.key === 'vasp' ? 1 : 0));
    State2.engines = engines;
    State2.capabilities = Object.fromEntries(engines.map(row => [row.key, row]));
    sel.innerHTML = engines.map(e =>
      '<option value="' + VCS.esc(e.key) + '">' + VCS.esc(e.name) +
      (e.key === 'vasp' ? tr('runtime.generate.engine.recommended_complete', {},
        '（推荐·完整）', ' (recommended · complete)') : tr(
        'runtime.generate.engine.file_adapter', {}, '（文件级适配）', ' (file-level adapter)')) +
      '</option>').join('');
    sel.disabled = engines.length < 2;
    const modeChanged = State2.sceneKey !== sceneKey;
    State2.sceneKey = sceneKey;
    const preferred = (current && current.engine) || VCS.activeEngine ||
      (r && r.default) || engines[0].key;
    const has = engines.some(e => e.key === State2.engine);
    if (!has || modeChanged) {
      State2.engine = engines.some(e => e.key === preferred) ? preferred : engines[0].key;
    }
    sel.value = State2.engine;
    await selectEngine(sel.value, false);
  }
  function syncEngineTask() {
    const sel = $('eng-task');
    const cap = State2.capabilities[State2.engine] || {};
    if (!sel) return;
    const tasks = cap.tasks || [];
    sel.innerHTML = tasks.map(task =>
      `<option value="${VCS.esc(task.key)}">${VCS.esc(task.name)}</option>`).join('');
    const active = VCS.activeCalculation || '';
    if (tasks.some(task => task.key === active)) sel.value = active;
    else if (tasks.length) sel.value = tasks[0].key;
  }
  async function saveEngineTask() {
    const sel = $('eng-task');
    if (!sel || !sel.value || sel.value === VCS.activeCalculation) return;
    const r = await VCS.call('calculation_set', sel.value);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.generate.engine.task_switch_failed', {
        error: (r && r.error) || tr('runtime.generate.common.unknown', {}, '未知', 'Unknown'),
      }, '切换任务类型失败:{error}', 'Failed to switch task type: {error}'), 'failc');
      syncEngineTask();
      return;
    }
    if (VCS.applyCalculation) VCS.applyCalculation(r.active_calculation || sel.value);
  }
  function renderEngineCapability(key) {
    const cap = State2.capabilities[key] || {};
    const note = $('engine-capability-note');
    const title = $('engine-card-title');
    const sub = $('engine-card-sub');
    const top = $('engine-note');
    const summary = $('generate-engine-summary');
    const name = ((State2.engines.find(row => row.key === key) || {}).name || key.toUpperCase());
    if (title) title.textContent = tr('runtime.generate.engine.input_preparation', { name },
      '{name} 输入准备', '{name} input preparation');
    if (sub) sub.textContent = cap.input_contract && cap.result_contract
      ? tr('runtime.generate.engine.contracts', {
        input: cap.input_contract, result: cap.result_contract,
      }, '{input}；结果：{result}', '{input}; results: {result}') : (cap.summary || '');
    if (top) top.textContent = key === 'vasp'
      ? tr('runtime.generate.engine.vasp_default', {},
        'VASP 为默认完整工作流；下方仅显示当前任务需要的 VASP 入口',
        'VASP is the default complete workflow; only the VASP entry points needed for the current task are shown below')
      : tr('runtime.generate.engine.adapter_scope', {
        name, support: cap.support_label || tr(
          'runtime.generate.engine.file_adapter_label', {}, '文件级适配', 'File-level adapter'),
      }, '{name}：{support}；未接通的 VASP 专用任务已隐藏',
      '{name}: {support}; unsupported VASP-only tasks are hidden');
    if (summary) summary.textContent = key === 'vasp'
      ? tr('runtime.generate.engine.vasp_workflow', {},
        'VASP · 完整输入、提交、回收与结果工作流',
        'VASP · complete input, submission, retrieval, and results workflow')
      : tr('runtime.generate.engine.workflow', {
        name, input: cap.input_contract || tr(
          'runtime.generate.engine.dedicated_input', {}, '专属输入', 'Dedicated input'),
      }, '{name} · {input} → 登记 → 提交 → 回收',
      '{name} · {input} → register → submit → retrieve');
    if (note) {
      const limits = cap.limitations || [];
      note.hidden = key === 'vasp' || (!cap.summary && !limits.length);
      note.innerHTML = key === 'vasp' ? '' : `<b>${VCS.esc(cap.summary || '')}</b>` +
        limits.map(line => `<br>${VCS.esc(line)}`).join('');
    }
  }
  async function selectEngine(key, persist = true) {
    if (persist) {
      const saved = await VCS.call('engine_set', key);
      if (!saved || saved.ok === false || saved.error) {
        VCS.log(tr('runtime.generate.engine.switch_failed', {
          error: (saved && saved.error) || tr(
            'runtime.generate.common.unknown', {}, '未知', 'Unknown'),
        }, '切换计算引擎失败:{error}', 'Failed to switch compute engine: {error}'), 'failc');
        const sel = $('engine-select'); if (sel) sel.value = State2.engine;
        return;
      }
      key = saved.engine || key;
      if (saved.capability) State2.capabilities[key] = Object.assign(
        {}, State2.capabilities[key] || {}, saved.capability);
      if (saved.active_calculation && VCS.applyCalculation) {
        VCS.applyCalculation(saved.active_calculation);
      }
    }
    State2.engine = key;
    const sel = $('engine-select');
    if (sel && sel.value !== key) sel.value = key;
    if (VCS.applyEngine) VCS.applyEngine(key, State2.capabilities[key] || {});
    renderEngineCapability(key);
    syncEngineTask();
    const isGauss = (key === 'gaussian');
    // 非 VASP:展开「引擎参数」折叠分区,让简化表单可见
    const card = $('engine-card');
    if (card && key !== 'vasp') {
      if (VCS.ui && typeof VCS.ui.setAccordionOpen === 'function') {
        VCS.ui.setAccordionOpen(card, true, true);
      } else {
        card.setAttribute('data-open', '1');
        const toggle = card.querySelector(':scope > .acc-h > .acc-toggle');
        if (toggle) toggle.setAttribute('aria-expanded', 'true');
      }
    }
    // 通用简化表单:cp2k/castep 用;vasp 走上方四件套;gaussian 走专属分子面板
    const form = $('engine-form');
    if (form) form.hidden = (key === 'vasp' || isGauss);
    const gp = $('gauss-panel');
    if (gp) gp.hidden = !isGauss;
    if (isGauss && window.GaussMol && window.GaussMol.onShow) window.GaussMol.onShow();
    document.querySelectorAll('[data-engine-field]').forEach(row => {
      const engines = String(row.getAttribute('data-engine-field') || '').split(/\s+/);
      row.hidden = engines.indexOf(key) < 0;
    });
    const periodic = $('eng-periodic');
    const cap = State2.capabilities[key] || {};
    if (periodic && !isGauss) {
      const boundaries = cap.boundaries || ['periodic', 'molecule'];
      periodic.disabled = boundaries.length === 1;
      Array.from(periodic.options).forEach(option => {
        const boundary = option.value === '1' ? 'periodic' : 'molecule';
        option.hidden = boundaries.indexOf(boundary) < 0;
      });
      const currentBoundary = periodic.value === '1' ? 'periodic' : 'molecule';
      if (boundaries.indexOf(currentBoundary) < 0) {
        periodic.value = boundaries.indexOf('periodic') >= 0 ? '1' : '0';
      }
    }
    const banner = $('engine-nonequiv');
    if (banner) {
      if (key === 'vasp') { banner.hidden = true; }
      else {
        const nr = await VCS.call('engine_nonequiv', 'vasp', key);
        const rep = (nr && nr.report) || [];
        banner.hidden = !rep.length;
        banner.innerHTML = `<b>${VCS.esc(tr(
          'runtime.generate.engine.nonequivalence', {},
          '跨引擎不等价(需逐项人工确认):',
          'Cross-engine nonequivalence (manual confirmation required for each item):',
        ))}</b><br>` +
          rep.map(x => VCS.esc(x)).join('<br>');
      }
    }
  }
  async function engineGenerate() {
    const eng = State2.engine;
    const out = val('eng-out');
    if (!val('eng-poscar')) {
      VCS.log(tr('runtime.generate.engine.poscar_required', {},
        '引擎:请选择结构 POSCAR', 'Engine: select a structure POSCAR'), 'failc'); return;
    }
    if (!out) {
      VCS.log(tr('runtime.generate.engine.output_required', {},
        '引擎:请选择输出目录', 'Engine: select an output directory'), 'failc'); return;
    }
    if (eng !== 'cp2k' && eng !== 'castep') {
      VCS.log(tr('runtime.generate.engine.use_dedicated_panel', {},
        '请使用当前引擎的专属输入面板',
        'Use the dedicated input panel for the current engine'), 'failc'); return;
    }
    const periodic = (val('eng-periodic') || '1') === '1';
    const strictNumber = raw => {
      const text = String(raw == null ? '' : raw).trim();
      if (!text) return null;
      const number = Number(text);
      return Number.isFinite(number) ? number : null;
    };
    const strictInteger = raw => {
      const number = strictNumber(raw);
      return number !== null && Number.isInteger(number) ? number : null;
    };
    const grid = id => {
      const tokens = val(id).split(/[\s,]+/).filter(Boolean);
      if (tokens.length !== 3) return null;
      const numbers = tokens.map(strictInteger);
      return numbers.every(number => number !== null && number > 0) ? numbers : null;
    };
    const multiplicity = strictInteger(val('eng-multiplicity') || '1');
    const charge = strictInteger(val('eng-charge') || '0');
    if (multiplicity === null || multiplicity < 1) {
      VCS.log(tr('runtime.generate.engine.multiplicity_invalid', {},
        '自旋多重度必须是正整数（2S+1）',
        'Spin multiplicity must be a positive integer (2S+1)'), 'failc'); return;
    }
    if (charge === null) {
      VCS.log(tr('runtime.generate.engine.charge_invalid', {},
        '体系净电荷必须是整数', 'The system net charge must be an integer'), 'failc'); return;
    }
    const kpts = eng === 'cp2k' ? grid('eng-cp2k-kpts') : grid('eng-kpts');
    const extras = {};
    if (eng === 'cp2k') {
      const cutoffRy = strictNumber(val('eng-cutoff-ry'));
      const relCutoffRy = strictNumber(val('eng-rel-cutoff-ry'));
      if (!(cutoffRy > 0) || !(relCutoffRy > 0)) {
        VCS.log(tr('runtime.generate.engine.cp2k_cutoff_invalid', {},
          'CP2K：CUTOFF 与 REL_CUTOFF 必须是正数（单位 Ry）',
          'CP2K: CUTOFF and REL_CUTOFF must be positive numbers in Ry'), 'failc'); return;
      }
      if (periodic && !kpts) {
        VCS.log(tr('runtime.generate.engine.cp2k_kpoints_invalid', {},
          'CP2K：周期计算请输入三个正整数 k 点网格',
          'CP2K: enter a three-positive-integer k-point grid for periodic calculations'), 'failc'); return;
      }
      extras.cutoff_ry = cutoffRy;
      extras.rel_cutoff_ry = relCutoffRy;
      if (val('eng-basis-file')) extras.basis_set_file = val('eng-basis-file');
      if (val('eng-potential-file')) extras.potential_file = val('eng-potential-file');
    }
    const castepCutoff = strictNumber(val('eng-cutoff'));
    if (eng === 'castep' && periodic && !(castepCutoff > 0)) {
      VCS.log(tr('runtime.generate.engine.castep_cutoff_invalid', {},
        'CASTEP：周期计算必须填写正数 cut_off_energy（eV）',
        'CASTEP: periodic calculations require a positive cut_off_energy in eV'), 'failc'); return;
    }
    if (eng === 'castep' && periodic && !kpts) {
      VCS.log(tr('runtime.generate.engine.castep_kpoints_invalid', {},
        'CASTEP：周期计算请输入三个正整数 k 点网格',
        'CASTEP: enter a three-positive-integer k-point grid for periodic calculations'), 'failc'); return;
    }
    const params = {
      poscar: val('eng-poscar'), task: val('eng-task') || 'relax',
      functional: val('eng-func') || 'PBE',
      dispersion: val('eng-disp') || null,
      cutoff_ev: eng === 'castep' ? castepCutoff : null,
      kpoints: periodic ? kpts : null,
      periodic: periodic,
      spin: ($('eng-spin') ? $('eng-spin').checked : false) || multiplicity > 1,
      charge: charge,
      multiplicity: multiplicity, extras: extras,
      calc_type: periodic ? 'slab' : 'molecule',
    };
    const btn = $('engine-gen-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.generate.engine.generating', { engine: eng },
      '生成 {engine} 引擎输入(文件级适配)…',
      'Generating {engine} engine input with the file-level adapter…'));
    try {
      const r = await VCS.call('engine_generate', eng, params, out);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.generate.engine.failed', {
          error: (r && r.error) || tr(
            'runtime.generate.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '引擎生成失败:{error}', 'Engine input generation failed: {error}'), 'failc'); return;
      }
      (r.files || []).forEach(f => VCS.log(tr(
        'runtime.generate.engine.file_created', { file: f },
        '已生成:{file}', 'Generated: {file}'), 'okc'));
      (r.issues || []).forEach(i => VCS.log(tr(
        'runtime.generate.engine.validation_issue', { issue: i },
        '自洽校验:{issue}', 'Consistency check: {issue}'), 'warnc'));
      (r.warnings || []).forEach(w => VCS.log(w, 'warnc'));
      if (!r.registered) {
        VCS.log(tr('runtime.generate.engine.registration_failed', { engine: eng },
          '{engine} 输入文件已生成，但 job.yaml/台账登记失败；已打开目录，请先处理日志中的登记问题，不能直接提交',
          '{engine} input files were generated, but job.yaml/ledger registration failed. The directory was opened; resolve the registration issue in the log before submission.'), 'failc');
        VCS.call('open_dir', out);
        VCS.toast(tr('runtime.generate.engine.generated_unregistered', {},
          '输入已生成，但尚未纳管', 'Input generated but not registered'), 'fail');
        return;
      }
      VCS.log(tr('runtime.generate.engine.registered', { engine: eng },
        '{engine} 引擎输入已生成并加入任务列表',
        '{engine} engine input generated and added to the job list'), 'okc');
      VCS.toast(tr('runtime.generate.engine.complete', { engine: eng },
        '已生成并纳管 {engine} 作业', 'Generated and registered the {engine} job'));
      if (VCS.nextStep) VCS.nextStep({
        title: tr('runtime.generate.engine.next_step_title', { engine: eng.toUpperCase() },
          '{engine} 作业已就绪', '{engine} job is ready'),
        message: tr('runtime.generate.engine.next_step_message', {},
          '输入文件、job.yaml 和输出下载契约已生成。',
          'Input files, job.yaml, and the output-download contract were generated.'),
        detail: tr('runtime.generate.engine.next_step_detail', {},
          '下一步：前往任务页选择服务器并提交；完成后可按该引擎解析能量和生成报告。',
          'Next: select a server and submit from Jobs. After completion, parse energies and generate reports for this engine.'),
        primaryLabel: tr('runtime.generate.engine.next_step_primary', {},
          '前往任务页提交', 'Open Jobs to submit'), page: 'jobs', focusJobDir: out,
      });
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
    sel.innerHTML = `<option value="">${VCS.esc(tr(
      'runtime.generate.campaign.no_template', {},
      '不使用模板(仅建吸附能项目矩阵)',
      'Do not use a template (build only the adsorption-energy project matrix)',
    ))}</option>` +
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
      VCS.esc(tr('runtime.generate.campaign.description', {
        stages: t.n_stages, analyses: (t.analyses || []).join(', '),
        scenario: t.figures_scenario || '—',
      }, '阶段数 {stages} · 汇总 {analyses} · 图场景 {scenario}',
      'Stages: {stages} · analyses: {analyses} · figure scenario: {scenario}'));
  }
  async function instantiateCampaign() {
    const sel = $('camp-tpl-sel');
    const key = sel ? sel.value : '';
    if (!key) {
      VCS.log(tr('runtime.generate.campaign.template_required', {},
        '请先选计算活动模板', 'Select a calculation campaign template first'), 'failc'); return;
    }
    const p = sacParams();
    const out = val('sac-out');
    if (!p.metals.length || !p.templates.length) {
      VCS.log(tr('runtime.generate.campaign.matrix_required', {},
        '请先在下方选金属与配位模板',
        'Select metals and coordination templates below first'), 'failc'); return;
    }
    if (!out) {
      VCS.log(tr('runtime.generate.campaign.output_required', {},
        '请选输出根目录', 'Select an output root'), 'failc'); return;
    }
    const systems = [];
    p.metals.forEach(m => p.templates.forEach(t => systems.push(m + '@' + t)));
    const spec = { systems: systems, adsorbates: p.ads, clean: true };
    if (!await VCS.confirm(tr('runtime.generate.campaign.confirm', {
      template: sel.options[sel.selectedIndex].text, count: systems.length,
    }, '将按模板「{template}」生成 {count} 体系的全链任务 DAG(relax→静态→频率→汇总)。继续?',
    'Generate a full-chain task DAG for {count} systems using template “{template}” (relax → static → frequency → summary)?'))) return;
    VCS.log(tr('runtime.generate.campaign.instantiating', {},
      '按模板实例化全链 DAG…', 'Instantiating the full-chain DAG from the template…'));
    const r = await VCS.call('campaign_instantiate', key, spec, out, null);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.generate.campaign.failed', {
        error: (r && r.error) || tr('runtime.generate.common.unknown', {}, '未知', 'Unknown'),
      }, '实例化失败:{error}', 'Instantiation failed: {error}'), 'failc'); return;
    }
    const est = (r.estimate && r.estimate.total) || 0;
    VCS.log(tr('runtime.generate.campaign.created', {
      jobs: r.n_jobs, hours: est, path: r.campaign_dir,
    }, '已生成全链 DAG:{jobs} 个作业,约 {hours} 核时 → {path}',
    'Full-chain DAG generated: {jobs} jobs, approximately {hours} core-hours → {path}'), 'okc');
    VCS.toast(tr('runtime.generate.campaign.complete', {},
      '已按模板生成全链批次', 'Full-chain campaign generated from the template'));
  }

  // ── 初始化:回填上次路径 + 首帧预览,绑定浏览/生成/输入监听 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }

  async function init() {
    wire('gen-poscar-btn', () => pickFile('gen-poscar', 'poscar'));
    wire('gen-poscar-3d', () => {
      const p = val('gen-poscar');
      if (!p) {
        VCS.log(tr('runtime.generate.preview.poscar_required', {},
          '请先选择 POSCAR 文件', 'Select a POSCAR file first'), 'failc'); return;
      }
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
    // 隐式溶剂化(VASPsol):勾选/改介电常数即预览将写入的键 + 补丁编译 warning
    { const el = $('gen-solvation'); if (el) el.addEventListener('change', refreshSolvationNote); }
    { const el = $('gen-solvation-ebk'); if (el) el.addEventListener('input', refreshSolvationNote); }

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
    // 金属 slab 建模:晶面目录 + 元素/晶面联动 + 浏览/生成
    loadMetalSlabCatalog();
    { const el = $('mslab-surface'); if (el) el.addEventListener('change', onMslabChange); }
    { const el = $('mslab-el');
      if (el) { el.addEventListener('input', onMslabChange); el.addEventListener('change', onMslabChange); } }
    wire('mslab-incar-btn', () => pickFile('mslab-incar', 'incar'));
    wire('mslab-out-btn', () => pickDir('mslab-out'));
    wire('mslab-gen-btn', mslabGenerate);
    // 多自旋并跑:浏览/生成
    wire('spin-poscar-btn', () => pickFile('spin-poscar', 'poscar'));
    wire('spin-incar-btn', () => pickFile('spin-incar', 'incar'));
    wire('spin-out-btn', () => pickDir('spin-out'));
    wire('spin-gen-btn', spinGenerate);

    // 引擎选择器(生成页):载入引擎下拉(VASP 优先)+ 浏览/生成
    { const esel = $('engine-select'); if (esel) esel.addEventListener('change', () => selectEngine(esel.value)); }
    loadEngines();
    wire('eng-poscar-btn', () => pickFile('eng-poscar', 'poscar'));
    wire('eng-out-btn', () => pickDir('eng-out'));
    wire('engine-gen-btn', engineGenerate);
    { const task = $('eng-task'); if (task) task.addEventListener('change', saveEngineTask); }

    const st = await VCS.call('gen_state');
    if (st) {
      setVal('gen-poscar', st.poscar);
      setVal('gen-incar', st.incar);
      setVal('gen-out', st.out_dir);
      setVal('gen-lib', st.lib_root);
      if (st.error) VCS.log(tr('runtime.generate.state.load_failed', { error: st.error },
        '读取上次路径失败:{error}', 'Failed to load previous paths: {error}'), 'failc');
    }
    refreshPreview();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // 工作模式晚于页面脚本加载：切换后重拉严格白名单，并应用该模式默认体系类型。
  document.addEventListener('vcs:scenario', e => {
    const sc = e.detail && e.detail.scenario;
    const defaults = (sc && sc.defaults) || {};
    const calc = $('gen-calc');
    if (calc && defaults.calc_type && Array.from(calc.options).some(o => o.value === defaults.calc_type)) {
      calc.value = defaults.calc_type;
      refreshPreview();
    }
    loadEngines();
  });
  document.addEventListener('vcs:calculation', syncEngineTask);
  document.addEventListener('vcs:language', () => {
    renderPreviewLanguage();
    renderSolvationNote();
    renderChips('sac-metals', SAC_METALS, chipVals('sac-metals'));
    renderChips('sac-templates', SAC_TEMPLATES, chipVals('sac-templates'));
    renderEngineCapability(State2.engine);
    syncEngineTask();
    onMslabChange();
    onCampTplChange();
  });
  document.addEventListener('vcs:engine', e => {
    const engine = e.detail && e.detail.engine;
    if (engine && engine !== State2.engine) loadEngines();
  });

  // 供 molbuild.js(①分子建模「下一步」)携分子进 Gaussian 面板:选 Gaussian 引擎 + 载分子
  async function useMolecule(struct) {
    await loadEngines();
    await selectEngine('gaussian');
    if (window.GaussMol && typeof window.GaussMol.useMolecule === 'function') {
      window.GaussMol.useMolecule(struct);
    }
  }

  window.Generate = { reload: () => refreshPreview(), useMolecule,
    selectEngine: async key => { await loadEngines(); return selectEngine(key); } };
})();
