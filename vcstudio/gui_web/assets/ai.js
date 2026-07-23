// ai.js — AI 助手页(⑥):论文 → 规格表 → 计划 → 实例化。LLM 只做文本抽取(每格带出处),
// 规格表可编辑;计划 = 纯计划对象;实例化经机时闸 + 单点先行 + 三态门禁。只依赖 app.js 的
// VCS.* 与 api 桥(ai_extract/ai_plan/ai_instantiate/settings_get)。全部插值走 VCS.esc;零 emoji。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const METHOD_FIELDS = ['functional', 'dispersion', 'encut', 'kpoints_relax',
    'kpoints_static', 'ediff', 'ediffg', 'spin', 'u_values', 'solvation'];
  const State = {
    spec: null, plan: null, tables: null, compareProj: null, variants: null,
    chatSession: null, chatMessages: [], chatAttachments: [],
    chatSelected: new Set(), chatBusy: false,
  };

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

  // ── 对话式助手:持久会话 + 明确勾选附件 + 本地限定命令 ──
  function setChatBusy(value) {
    State.chatBusy = !!value;
    const send = $('ai-chat-send'), stop = $('ai-chat-stop'), input = $('ai-chat-input');
    if (send) send.disabled = State.chatBusy;
    if (stop) stop.disabled = !State.chatBusy;
    if (input) input.disabled = State.chatBusy;
  }

  function renderChat() {
    const box = $('ai-chat-messages');
    if (!box) return;
    const messages = State.chatMessages || [];
    if (!messages.length) {
      box.innerHTML = '<div class="ai-chat-empty">输入 /help 查看离线命令，或添加文件后开始分析。</div>';
    } else {
      box.innerHTML = messages.map(item => {
        const role = item.role === 'user' ? 'user' : 'assistant';
        const who = role === 'user' ? '你' : (item.source === 'local' ? '本地助手' : 'AI 助手');
        const files = (item.attachments || []).map(a => a.name).filter(Boolean);
        const fileLine = files.length
          ? `<span class="ai-msg-meta">附件：${files.map(VCS.esc).join('、')}</span>` : '';
        return `<div class="ai-msg ${role}"><span class="ai-msg-meta">${VCS.esc(who)}</span>` +
          fileLine + `${VCS.esc(item.content || '')}</div>`;
      }).join('');
      if (State.chatBusy) {
        box.insertAdjacentHTML('beforeend',
          '<div class="ai-msg assistant"><span class="ai-msg-meta">AI 助手</span>正在生成，可随时停止…</div>');
      }
    }
    box.scrollTop = box.scrollHeight;
    renderChatFiles();
  }

  function renderChatFiles() {
    const box = $('ai-chat-files');
    if (!box) return;
    const files = State.chatAttachments || [];
    if (!files.length) {
      box.innerHTML = '<span class="sub">本会话暂无附件</span>';
      return;
    }
    box.innerHTML = files.map(file => {
      const checked = State.chatSelected.has(file.id) ? ' checked' : '';
      const kb = Math.max(1, Math.ceil((Number(file.size_bytes) || 0) / 1024));
      return `<label class="ai-file-chip" title="${VCS.esc(file.preview_note || '')}">` +
        `<input type="checkbox" data-chat-file="${VCS.esc(file.id)}"${checked}>` +
        `${VCS.esc(file.name || '附件')} · ${kb} KB</label>`;
    }).join('');
  }

  async function loadChatHistory(sessionId, clearSelection) {
    if (!sessionId) return;
    if (clearSelection) State.chatSelected = new Set();
    const r = await VCS.call('ai_chat_history', sessionId);
    if (!r || r.ok === false || r.error) {
      VCS.log('读取对话失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    State.chatSession = sessionId;
    State.chatMessages = r.messages || [];
    State.chatAttachments = r.attachments || [];
    const valid = new Set(State.chatAttachments.map(item => item.id));
    State.chatSelected = new Set(
      Array.from(State.chatSelected).filter(id => valid.has(id)));
    renderChat();
  }

  async function loadChatSessions(createIfEmpty) {
    let r = await VCS.call('ai_chat_sessions');
    if (!r || r.ok === false || r.error) {
      VCS.log('读取 AI 会话失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    let sessions = r.sessions || [];
    if (!sessions.length && createIfEmpty) {
      const made = await VCS.call('ai_chat_new', '材料计算对话');
      if (!made || made.ok === false || made.error) {
        VCS.log('新建 AI 会话失败:' + ((made && made.error) || '未知错误'), 'failc');
        return;
      }
      sessions = [made.session];
    }
    const select = $('ai-chat-session');
    if (!select) return;
    const wanted = sessions.some(item => item.id === State.chatSession)
      ? State.chatSession : (sessions[0] && sessions[0].id);
    select.innerHTML = sessions.map(item =>
      `<option value="${VCS.esc(item.id)}">${VCS.esc(item.title || '新对话')}</option>`).join('');
    if (wanted) {
      select.value = wanted;
      await loadChatHistory(wanted, wanted !== State.chatSession);
    }
  }

  async function newChat() {
    const r = await VCS.call('ai_chat_new', '材料计算对话');
    if (!r || r.ok === false || r.error) {
      VCS.log('新建 AI 会话失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    State.chatSession = r.session.id;
    State.chatSelected = new Set();
    await loadChatSessions(false);
    const input = $('ai-chat-input'); if (input) input.focus();
  }

  async function attachChatFiles() {
    if (!State.chatSession) { await newChat(); }
    if (!State.chatSession) return;
    const picked = await VCS.call('pick_files', 'chat');
    if (!picked || picked.error) {
      VCS.log('选择附件失败:' + ((picked && picked.error) || '未知错误'), 'failc');
      return;
    }
    if (!(picked.paths || []).length) return;
    const r = await VCS.call('ai_chat_attach', State.chatSession, picked.paths);
    if (!r) {
      VCS.log('附件导入失败:后端没有返回结果', 'failc');
      return;
    }
    (r.attachments || []).forEach(item => State.chatSelected.add(item.id));
    if ((r.attachments || []).length) await loadChatHistory(State.chatSession, false);
    if (r.ok === false || r.error) {
      VCS.log('附件导入失败:' + ((r && r.error) || '未知错误'), 'failc');
      return;
    }
    VCS.log('附件已安全导入本地会话；本次发送默认勾选新附件', 'okc');
  }

  async function sendChat() {
    if (State.chatBusy) return;
    if (!State.chatSession) { await newChat(); }
    const input = $('ai-chat-input');
    const text = (input && input.value.trim()) || '';
    if (!text || !State.chatSession) return;
    document.querySelectorAll('[data-chat-file]').forEach(el => {
      if (el.checked) State.chatSelected.add(el.dataset.chatFile);
      else State.chatSelected.delete(el.dataset.chatFile);
    });
    const selected = Array.from(State.chatSelected);
    setChatBusy(true);
    renderChat();
    try {
      const r = await VCS.call('ai_chat_send', State.chatSession, text, selected);
      if (!r || r.ok === false || r.error) {
        VCS.log('AI 对话失败:' + ((r && r.error) || '未知错误'), 'failc');
      } else {
        if (input) input.value = '';
        State.chatSelected = new Set();
      }
    } finally {
      setChatBusy(false);
      await loadChatHistory(State.chatSession, false);
      await loadChatSessions(false);
    }
  }

  async function stopChat() {
    if (!State.chatSession) return;
    const r = await VCS.call('ai_chat_stop', State.chatSession);
    if (r && r.result && r.result.stopped) VCS.toast('已停止当前 AI 回复；集群作业不受影响');
    else VCS.toast('当前没有正在生成的 AI 回复');
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

  // ── 能力一:数据对照(提取文献数据表 + 与项目计算值对照) ──
  async function extractTables() {
    const source = (($('ai-pdf') && $('ai-pdf').value.trim()) || '') ||
      (($('ai-text') && $('ai-text').value.trim()) || '');
    if (!source) { VCS.log('请先提供 PDF 路径或粘贴论文文本', 'failc'); return; }
    VCS.log('提取文献数据表(LLM 只誊抄印着的数值 + 确定性校验)…');
    const r = await VCS.call('ai_extract_tables', source);
    if (!r || r.ok === false || r.error) { VCS.log('提取数据表失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    State.tables = r.tables || [];
    renderTables();
    VCS.log('已提取 ' + State.tables.length + ' 张数据表(仅供对照,绝不回流计算)', 'okc');
  }
  function renderTables() {
    const box = $('ai-tables');
    if (!box) return;
    if (!State.tables || !State.tables.length) { box.innerHTML = '<span class="sub">未提取到数据表</span>'; return; }
    let h = '';
    State.tables.forEach(t => {
      h += `<div class="sub" style="margin:6px 0 2px"><b>${VCS.esc(t.label || '表')}</b> · ${VCS.esc(t.kind || '')}</div>`;
      h += '<table class="ai-tbl"><thead><tr><th>体系</th><th>物种</th><th>文献值/eV</th><th>页码</th></tr></thead><tbody>';
      (t.rows || []).forEach(r => {
        h += `<tr><td>${VCS.esc(r.system || '')}</td><td>${VCS.esc(r.species || '')}</td>` +
          `<td class="num">${r.value_ev == null ? '—' : r.value_ev}</td>` +
          `<td class="num" title="页码">${r.page_hint == null ? '—' : r.page_hint}</td></tr>`;
      });
      h += '</tbody></table>';
    });
    box.innerHTML = h;
  }
  async function compareLit() {
    const proj = ($('ai-cmp-project') && $('ai-cmp-project').value) || '';
    if (!proj) { VCS.log('请选对照项目', 'failc'); return; }
    if (!State.tables || !State.tables.length) { VCS.log('请先提取文献数据表', 'failc'); return; }
    VCS.log('与文献对照(体系×物种×量对齐,纯确定性)…');
    const r = await VCS.call('ai_compare', proj, State.tables);
    const out = $('ai-compare-out');
    if (!r || r.ok === false || r.error) { VCS.log('对照失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    State.compareProj = proj;
    let h = `<div class="ai-metrics"><span>比对项 <b>${r.n}</b></span>` +
      `<span>MAE <b>${r.mae == null ? '—' : (+r.mae).toFixed(3)}</b> eV</span>` +
      `<span>RMSE <b>${r.rmse == null ? '—' : (+r.rmse).toFixed(3)}</b> eV</span></div>`;
    if ((r.worst || []).length) h += '<div class="ai-worst sub">偏差最大:' +
      r.worst.map(w => `${VCS.esc(w.system)}/${VCS.esc(w.species)}(Δ${(+w.delta).toFixed(2)})`).join('、') + '</div>';
    h += '<table class="ai-tbl"><thead><tr><th>体系</th><th>物种</th><th>文献/eV</th><th>计算/eV</th><th>差/eV</th></tr></thead><tbody>';
    (r.pairs || []).forEach(p => {
      h += `<tr><td>${VCS.esc(p.system)}</td><td>${VCS.esc(p.species)}</td>` +
        `<td class="num">${(+p.ref).toFixed(3)}</td><td class="num">${(+p.ours).toFixed(3)}</td>` +
        `<td class="num">${(+p.delta).toFixed(3)}</td></tr>`;
    });
    h += '</tbody></table><div class="sub" style="margin-top:6px">' + VCS.esc(r.summary || '') + '</div>';
    h += '<div class="actions" style="margin-top:8px"><button class="btn" id="ai-write-val">写入 validation.md</button></div>';
    if (out) out.innerHTML = h;
    const wb = $('ai-write-val');
    if (wb) wb.addEventListener('click', writeValidation);
    VCS.log('对照完成:' + (r.summary || ''), 'okc');
  }
  async function writeValidation() {
    if (!State.compareProj || !State.tables) return;
    const r = await VCS.call('ai_write_validation', State.compareProj, State.tables);
    if (!r || r.ok === false || r.error) { VCS.log('写入 validation.md 失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    VCS.log('已写入 ' + r.path, 'okc');
    VCS.toast('validation.md 已写入');
  }

  // ── 能力二:材料变体 ──
  async function suggestVariants() {
    VCS.log('推荐材料变体(母版邻域 · 排除已算组合)…');
    const r = await VCS.call('ai_variants', buildSpec(), null);
    const out = $('ai-variants-out');
    if (!r || r.ok === false || r.error) { VCS.log('推荐变体失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    State.variants = r;
    let h = '';
    (r.variants || []).forEach(v => {
      h += `<div class="ai-var-row"><span class="vk">${VCS.esc(v.kind)}</span>` +
        `<b>${VCS.esc(v.metal)}@${VCS.esc(v.template)}</b><span class="vd">${VCS.esc(v.rationale_zh || '')}</span>` +
        `<span class="sub">P${v.priority}</span></div>`;
    });
    const plan = r.plan || {};
    h += `<div class="sub" style="margin-top:8px">${VCS.esc(plan.note || '')}</div>`;
    (plan.batches || []).forEach(b => {
      h += `<div class="sub">批 ${b.batch}(${VCS.esc(b.reason || '')}):${(b.jobs || []).join('、')} · ~${b.estimate_hours} 核时</div>`;
    });
    if (out) out.innerHTML = h;
    const gen = $('ai-variants-gen'); if (gen) gen.hidden = !(r.variants || []).length;
    VCS.log('已推荐 ' + (r.variants || []).length + ' 个变体', 'okc');
  }
  async function genVariantMatrix() {
    if (!State.variants || !State.variants.matrix_spec) return;
    const ms = State.variants.matrix_spec;
    if (!await VCS.confirm('将把变体矩阵(' + (ms.metals || []).join(',') + ' × ' +
      (ms.templates || []).join(',') + ')带入①结构建模的 SAC 批量建模,请补 INCAR/输出根后生成。继续?')) return;
    const more = document.getElementById('sac-metal-more');
    if (more) more.value = (ms.metals || []).join(', ');
    const nav = document.querySelector('nav a[data-page="structure"]');
    if (nav) nav.click();
    VCS.toast('已带入 SAC 批量建模(补 INCAR/输出根后生成矩阵)');
    VCS.log('变体矩阵已带入 SAC 建模:金属 ' + (ms.metals || []).join('、'), 'okc');
  }

  // ── 能力三:论文草稿 ──
  async function genManuscript() {
    const proj = ($('ai-ms-project') && $('ai-ms-project').value) || '';
    if (!proj) { VCS.log('请选项目', 'failc'); return; }
    const fmt = ($('ai-ms-fmt') && $('ai-ms-fmt').value) || 'markdown';
    VCS.log('生成论文骨架(Methods 全自动 · Results 逐图数据句)…');
    const r = await VCS.call('ai_manuscript', proj, fmt);
    const out = $('ai-ms-out');
    if (!r || r.ok === false || r.error) { VCS.log('生成论文骨架失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    const st = r.stats || {};
    let h = '<div style="margin:6px 0">' +
      `<span class="ai-stat-pill">自动句 <b>${st.auto == null ? '—' : st.auto}</b></span>` +
      `<span class="ai-stat-pill">占位待补 <b>${st.placeholder == null ? '—' : st.placeholder}</b></span>` +
      `<span class="ai-stat-pill">自动率 <b>${st.auto_ratio == null ? '—' : Math.round(st.auto_ratio * 100) + '%'}</b></span></div>`;
    h += `<div class="sub">章节:${(r.sections || []).join(' · ')}</div>`;
    if (!r.docx_available && fmt === 'docx') h += `<div class="sub" style="color:var(--warn)">${VCS.esc(r.note || '未安装 python-docx,已降级只出 .md')}</div>`;
    h += `<div class="actions" style="margin-top:8px"><button class="btn" data-open="${VCS.esc(r.path || '')}">打开产物</button></div>`;
    if (out) out.innerHTML = h;
    VCS.log('论文骨架已生成:自动 ' + (st.auto || 0) + ' 句 / 占位 ' + (st.placeholder || 0) + ' 处(诚实呈现)', 'okc');
  }

  async function loadProjects() {
    const r = await VCS.call('proj_list');
    const projs = (r && r.projects) || [];
    const opts = '<option value="">(选项目)</option>' +
      projs.map(p => `<option value="${VCS.esc(p.path)}">${VCS.esc(p.name || p.path)}</option>`).join('');
    ['ai-cmp-project', 'ai-ms-project'].forEach(id => { const s = $(id); if (s) s.innerHTML = opts; });
  }

  // ── 初始化 ──
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }
  let inited = false;
  function init() {
    if (inited) return; inited = true;
    wire('ai-chat-new', newChat);
    wire('ai-chat-attach', attachChatFiles);
    wire('ai-chat-send', sendChat);
    wire('ai-chat-stop', stopChat);
    const chatSession = $('ai-chat-session');
    if (chatSession) chatSession.addEventListener('change', () => {
      loadChatHistory(chatSession.value, true);
    });
    const chatFiles = $('ai-chat-files');
    if (chatFiles) chatFiles.addEventListener('change', e => {
      const input = e.target.closest && e.target.closest('[data-chat-file]');
      if (!input) return;
      if (input.checked) State.chatSelected.add(input.dataset.chatFile);
      else State.chatSelected.delete(input.dataset.chatFile);
    });
    const chatInput = $('ai-chat-input');
    if (chatInput) chatInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault(); sendChat();
      }
    });
    wire('ai-pdf-btn', pickPdf);
    wire('ai-extract-btn', extract);
    wire('ai-plan-btn', plan);
    wire('ai-outroot-btn', pickOutRoot);
    wire('ai-inst-btn', instantiate);
    wire('ai-tables-btn', extractTables);
    wire('ai-compare-btn', compareLit);
    wire('ai-cmp-refresh', loadProjects);
    wire('ai-variants-btn', suggestVariants);
    wire('ai-variants-gen', genVariantMatrix);
    wire('ai-ms-btn', genManuscript);
    const msout = $('ai-ms-out');
    if (msout) msout.addEventListener('click', e => {
      const b = e.target.closest('[data-open]'); if (b && b.dataset.open) VCS.call('open_dir', b.dataset.open);
    });
    const auto = $('ai-autopilot');
    if (auto) auto.addEventListener('change', () => {
      const w = $('ai-autowarn'); if (w) w.hidden = !auto.checked;
    });
    refreshGuide();
    loadProjects();
    loadChatSessions(true);
  }

  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'ai') { init(); refreshGuide(); loadProjects(); }
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.AIAssistant = { reload: () => {
    refreshGuide(); loadChatSessions(false);
  } };
})();
