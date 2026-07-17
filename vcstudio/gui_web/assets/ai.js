// ai.js — AI 助手页(⑥):论文 → 规格表 → 计划 → 实例化。LLM 只做文本抽取(每格带出处),
// 规格表可编辑;计划 = 纯计划对象;实例化经机时闸 + 单点先行 + 三态门禁。只依赖 app.js 的
// VCS.* 与 api 桥(ai_extract/ai_plan/ai_instantiate/settings_get)。全部插值走 VCS.esc;零 emoji。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const METHOD_FIELDS = ['functional', 'dispersion', 'encut', 'kpoints_relax',
    'kpoints_static', 'ediff', 'ediffg', 'spin', 'u_values', 'solvation'];
  const State = { spec: null, plan: null };

  function leafVal(leaf) {
    if (leaf && typeof leaf === 'object') {
      const v = (leaf.normalized != null) ? leaf.normalized : leaf.value;
      return (v == null) ? '' : (Array.isArray(v) ? v.join(' ') : String(v));
    }
    return (leaf == null) ? '' : String(leaf);
  }
  function leafList(list) {
    return (list || []).map(leafVal).filter(v => v !== '').join(', ');
  }
  function leafOrigin(leaf) {
    const o = (leaf && leaf.origin) || {};
    if (o.page == null && !o.quote) return '';
    return `第 ${o.page == null ? '?' : o.page} 页:${o.quote || ''}`;
  }
  function isLow(leaf) {
    return leaf && typeof leaf === 'object' && leaf.confidence && leaf.confidence !== 'high';
  }

  // ── 联网门控引导条 ──
  async function refreshGuide() {
    const s = await VCS.call('settings_get');
    const on = s && s.llm && s.llm.allow_external;
    const g = $('ai-guide');
    if (g) g.hidden = !!on;
  }

  // ── 步骤 1:提取规格表 ──
  async function pickPdf() {
    const r = await VCS.call('pick_file', 'pdf');
    if (r && r.error) { VCS.log('选择 PDF 失败:' + r.error, 'failc'); return; }
    if (r && r.path && $('ai-pdf')) $('ai-pdf').value = r.path;
  }

  async function extract() {
    const pdf = ($('ai-pdf') && $('ai-pdf').value.trim()) || '';
    const text = ($('ai-text') && $('ai-text').value.trim()) || '';
    const source = pdf || text;
    if (!source) { VCS.log('请提供 PDF 路径或粘贴方法学文本', 'failc'); return; }
    const btn = $('ai-extract-btn');
    if (btn) btn.disabled = true;
    VCS.log('提取规格表(LLM 只抽文本,每格带出处)…');
    try {
      const r = await VCS.call('ai_extract', source);
      if (!r || r.ok === false || r.error) {
        VCS.log('提取失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      State.spec = r.spec || {};
      renderSpec();
      (r.issues || []).forEach(i => VCS.log('校验提示:' + i, 'warnc'));
      VCS.log('规格表已生成,可逐格编辑后「生成计划」', 'okc');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 步骤 2:可编辑规格表(分区:体系 / 方法 / 反应 / 产出) ──
  function renderSpec() {
    const box = $('ai-spec');
    const card = $('ai-spec-card');
    if (!box) return;
    const spec = State.spec || {};
    const sys = (spec.systems && spec.systems[0]) || {};
    let h = '';
    // 体系
    h += group('体系', [
      cellRow('substrate', '基底', leafVal(sys.substrate), leafOrigin(sys.substrate), isLow(sys.substrate)),
      cellRow('metals', '金属(逗号分隔)', leafList(sys.metals), '', false),
      cellRow('sites', '配位模板(逗号分隔)', leafList(sys.sites), '', false),
      cellRow('adsorbates', '吸附质(逗号分隔)', leafList(spec.adsorbates), '', false),
    ]);
    // 方法
    const methods = spec.methods || {};
    h += group('方法', METHOD_FIELDS.map(f =>
      cellRow('m_' + f, f, leafVal(methods[f]), leafOrigin(methods[f]), isLow(methods[f]))));
    // 反应
    h += group('反应', [cellRow('reactions', '反应预设(逗号分隔)', leafList(spec.reactions), '', false)]);
    // 产出
    h += group('产出', [
      cellRow('outputs', '图表类型(逗号分隔)', leafList(spec.outputs), '', false),
      cellRow('mode', '模式(reproduce/rebuild/design)', leafVal(spec.mode), leafOrigin(spec.mode), isLow(spec.mode)),
    ]);
    box.innerHTML = h;
    if (card) card.hidden = false;
  }

  function group(title, rows) {
    return `<div class="ai-spec-group"><div class="ai-spec-group-h">${VCS.esc(title)}</div>` +
      rows.join('') + '</div>';
  }
  function cellRow(id, label, value, origin, low) {
    return '<div class="ai-cell">' +
      `<span class="ai-cell-k">${VCS.esc(label)}</span>` +
      `<input class="ipt" id="ai-c-${id}" value="${VCS.esc(value)}">` +
      (low ? '<span class="ai-lowconf">低置信</span>' : '') +
      (origin ? `<span class="ai-origin" title="${VCS.esc(origin)}">出处</span>` : '') +
      '</div>';
  }

  function cv(id) { const el = $('ai-c-' + id); return el ? el.value.trim() : ''; }
  function cvList(id) { return cv(id).split(',').map(s => s.trim()).filter(Boolean).map(v => ({ value: v })); }

  // 编辑后的输入 → 规格表(每格包成 {value},plan_campaign 内部再过 validate_spec 归一)
  function buildSpec() {
    const methods = {};
    METHOD_FIELDS.forEach(f => { const v = cv('m_' + f); if (v) methods[f] = { value: v }; });
    return {
      systems: [{
        substrate: { value: cv('substrate') },
        metals: cvList('metals'),
        sites: cvList('sites'),
      }],
      adsorbates: cvList('adsorbates'),
      methods: methods,
      reactions: cvList('reactions'),
      outputs: cvList('outputs'),
      mode: { value: cv('mode') },
    };
  }

  // ── 步骤 3:生成计划 ──
  async function plan() {
    const btn = $('ai-plan-btn');
    if (btn) btn.disabled = true;
    VCS.log('生成实例化计划(纯计划对象,不落盘)…');
    try {
      const r = await VCS.call('ai_plan', buildSpec());
      if (!r || r.ok === false || r.error) {
        VCS.log('生成计划失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      State.plan = r.plan;
      renderPlan(r);
      VCS.log('计划已生成:约 ' + (r.jobs_estimate || 0) + ' 个作业', 'okc');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderPlan(r) {
    const box = $('ai-plan');
    const card = $('ai-plan-card');
    if (!box) return;
    const plan = r.plan || {};
    const m = plan.matrix || {};
    const nMetals = (m.metals || []).length, nTemplates = (m.templates || []).length;
    let h = '<div class="ai-plan-stat">' +
      `<span>矩阵规模:<b>${nMetals}</b> 金属 × <b>${nTemplates}</b> 模板</span>` +
      `<span>作业数:<b>${r.jobs_estimate || 0}</b></span>` +
      `<span>预估机时:<b>${r.est_core_hours == null ? '—' : r.est_core_hours}</b> 核时</span>` +
      `<span>单点先行:<b>${VCS.esc(plan.pilot || '—')}</b></span></div>`;
    (r.warnings || []).forEach(w => {
      h += `<div class="sub" style="color:var(--warn);margin:4px 0">⚠ ${VCS.esc(w)}</div>`;
    });
    box.innerHTML = h;
    if (card) card.hidden = false;
  }

  // ── 步骤 3b:实例化(单点先行)+ 全自动模式开关 ──
  async function pickOutRoot() {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log('选择目录失败:' + r.error, 'failc'); return; }
    if (r && r.path && $('ai-outroot')) $('ai-outroot').value = r.path;
  }

  async function instantiate() {
    if (!State.plan) { VCS.log('请先生成计划', 'failc'); return; }
    const root = ($('ai-outroot') && $('ai-outroot').value.trim()) || '';
    if (!root) { VCS.log('请选择实例化输出根目录', 'failc'); return; }
    const auto = $('ai-autopilot') && $('ai-autopilot').checked;
    const msg = auto
      ? '全自动模式:仍受机时上限 + 单点先行 + 三态门禁约束,且需已在设置页允许外发。继续实例化?'
      : '将实例化计划(先释放代表作业,单点先行)。继续?';
    if (!await VCS.confirm(msg)) return;
    const btn = $('ai-inst-btn');
    if (btn) btn.disabled = true;
    VCS.log('实例化中(门禁:机时闸 → 单点先行 → 写 campaign → 账本)…');
    try {
      const r = await VCS.call('ai_instantiate', State.plan, root, {});
      if (!r || r.ok === false || r.error) {
        VCS.log('实例化失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      VCS.log('已实例化 ' + (r.created || []).length + ' 个作业节点', 'okc');
      if (r.campaign_dir) VCS.log('campaign:' + r.campaign_dir, 'okc');
      if (r.awaiting === 'pilot_validation') {
        VCS.log('单点先行:代表作业 ' + ((r.pilot && r.pilot.id) || '') +
          ' 先跑,验证通过后再放行 fan-out', 'okc');
      }
      VCS.toast('已实例化(单点先行)');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    wire('ai-pdf-btn', pickPdf);
    wire('ai-extract-btn', extract);
    wire('ai-plan-btn', plan);
    wire('ai-outroot-btn', pickOutRoot);
    wire('ai-inst-btn', instantiate);
    const auto = $('ai-autopilot');
    if (auto) auto.addEventListener('change', () => {
      const w = $('ai-autowarn'); if (w) w.hidden = !auto.checked;
    });
    refreshGuide();
  }

  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'ai') { init(); refreshGuide(); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.AIAssistant = { reload: refreshGuide };
})();
