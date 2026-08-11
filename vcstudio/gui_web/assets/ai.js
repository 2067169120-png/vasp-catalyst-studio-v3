// ai.js — AI 助手页(⑥):论文 → 规格表 → 计划 → 实例化。LLM 只做文本抽取(每格带出处),
// 规格表可编辑;计划 = 纯计划对象;实例化经机时闸 + 单点先行 + 三态门禁。只依赖 app.js 的
// VCS.* 与 api 桥(ai_extract/ai_plan/ai_instantiate/settings_get)。全部插值走 VCS.esc;零 emoji。
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
  const METHOD_FIELDS = ['functional', 'dispersion', 'encut', 'kpoints_relax',
    'kpoints_static', 'ediff', 'ediffg', 'spin', 'u_values', 'solvation'];
  const State = {
    spec: null, plan: null, planResult: null, tables: null, compareProj: null,
    variants: null,
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
    return tr('runtime.ai.source.page_quote', {
      page: o.page == null ? '?' : o.page, quote: o.quote || '',
    }, '第 {page} 页:{quote}', 'Page {page}: {quote}');
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
      box.innerHTML = `<div class="ai-chat-empty">${VCS.esc(tr(
        'runtime.ai.chat.empty', {},
        '输入 /help 查看离线命令，或添加文件后开始分析。',
        'Enter /help to view offline commands, or add files to begin analysis.',
      ))}</div>`;
    } else {
      box.innerHTML = messages.map(item => {
        const role = item.role === 'user' ? 'user' : 'assistant';
        const who = role === 'user'
          ? tr('runtime.ai.chat.role.you', {}, '你', 'You')
          : (item.source === 'local'
            ? tr('runtime.ai.chat.role.local', {}, '本地助手', 'Local assistant')
            : tr('runtime.ai.chat.role.ai', {}, 'AI 助手', 'AI assistant'));
        const files = (item.attachments || []).map(a => a.name).filter(Boolean);
        const fileLine = files.length
          ? `<span class="ai-msg-meta">${VCS.esc(tr(
            'runtime.ai.chat.attachments', { files: files.map(VCS.esc).join(', ') },
            '附件：{files}', 'Attachments: {files}',
          ))}</span>` : '';
        return `<div class="ai-msg ${role}"><span class="ai-msg-meta">${VCS.esc(who)}</span>` +
          fileLine + `${VCS.esc(item.content || '')}</div>`;
      }).join('');
      if (State.chatBusy) {
        box.insertAdjacentHTML('beforeend',
          `<div class="ai-msg assistant"><span class="ai-msg-meta">${VCS.esc(tr(
            'runtime.ai.chat.role.ai', {}, 'AI 助手', 'AI assistant',
          ))}</span>${VCS.esc(tr(
            'runtime.ai.chat.generating', {}, '正在生成，可随时停止…',
            'Generating; you can stop at any time…',
          ))}</div>`);
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
      box.innerHTML = `<span class="sub">${VCS.esc(tr(
        'runtime.ai.chat.no_attachments', {}, '本会话暂无附件',
        'No attachments in this conversation',
      ))}</span>`;
      return;
    }
    box.innerHTML = files.map(file => {
      const checked = State.chatSelected.has(file.id) ? ' checked' : '';
      const kb = Math.max(1, Math.ceil((Number(file.size_bytes) || 0) / 1024));
      return `<label class="ai-file-chip" title="${VCS.esc(file.preview_note || '')}">` +
        `<input type="checkbox" data-chat-file="${VCS.esc(file.id)}"${checked}>` +
        `${VCS.esc(file.name || tr(
          'runtime.ai.chat.attachment_default_name', {}, '附件', 'Attachment',
        ))} · ${kb} KB</label>`;
    }).join('');
  }

  async function loadChatHistory(sessionId, clearSelection) {
    if (!sessionId) return;
    if (clearSelection) State.chatSelected = new Set();
    const r = await VCS.call('ai_chat_history', sessionId);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.chat.history_failed', {
        error: (r && r.error) || tr(
          'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '读取对话失败:{error}', 'Failed to read conversation: {error}'), 'failc');
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
      VCS.log(tr('runtime.ai.chat.sessions_failed', {
        error: (r && r.error) || tr(
          'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '读取 AI 会话失败:{error}', 'Failed to read AI conversations: {error}'), 'failc');
      return;
    }
    let sessions = r.sessions || [];
    if (!sessions.length && createIfEmpty) {
      const made = await VCS.call('ai_chat_new', tr(
        'runtime.ai.chat.default_title', {}, '材料计算对话',
        'Materials-computation conversation'));
      if (!made || made.ok === false || made.error) {
        VCS.log(tr('runtime.ai.chat.create_failed', {
          error: (made && made.error) || tr(
            'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '新建 AI 会话失败:{error}', 'Failed to create AI conversation: {error}'), 'failc');
        return;
      }
      sessions = [made.session];
    }
    const select = $('ai-chat-session');
    if (!select) return;
    const wanted = sessions.some(item => item.id === State.chatSession)
      ? State.chatSession : (sessions[0] && sessions[0].id);
    select.innerHTML = sessions.map(item =>
      `<option value="${VCS.esc(item.id)}">${VCS.esc(item.title || tr(
        'runtime.ai.chat.untitled', {}, '新对话', 'New conversation',
      ))}</option>`).join('');
    if (wanted) {
      select.value = wanted;
      await loadChatHistory(wanted, wanted !== State.chatSession);
    }
  }

  async function newChat() {
    const r = await VCS.call('ai_chat_new', tr(
      'runtime.ai.chat.default_title', {}, '材料计算对话',
      'Materials-computation conversation'));
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.chat.create_failed', {
        error: (r && r.error) || tr(
          'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '新建 AI 会话失败:{error}', 'Failed to create AI conversation: {error}'), 'failc');
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
      VCS.log(tr('runtime.ai.chat.pick_attachment_failed', {
        error: (picked && picked.error) || tr(
          'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '选择附件失败:{error}', 'Failed to select attachments: {error}'), 'failc');
      return;
    }
    if (!(picked.paths || []).length) return;
    const r = await VCS.call('ai_chat_attach', State.chatSession, picked.paths);
    if (!r) {
      VCS.log(tr('runtime.ai.chat.import_no_response', {},
        '附件导入失败:后端没有返回结果',
        'Attachment import failed: the backend returned no result'), 'failc');
      return;
    }
    (r.attachments || []).forEach(item => State.chatSelected.add(item.id));
    if ((r.attachments || []).length) await loadChatHistory(State.chatSession, false);
    if (r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.chat.import_failed', {
        error: (r && r.error) || tr(
          'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
      }, '附件导入失败:{error}', 'Attachment import failed: {error}'), 'failc');
      return;
    }
    VCS.log(tr('runtime.ai.chat.imported', {},
      '附件已安全导入本地会话；本次发送默认勾选新附件',
      'Attachments were safely imported into the local conversation and are selected for the next message'), 'okc');
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
        VCS.log(tr('runtime.ai.chat.send_failed', {
          error: (r && r.error) || tr(
            'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, 'AI 对话失败:{error}', 'AI conversation failed: {error}'), 'failc');
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
    if (r && r.result && r.result.stopped) VCS.toast(tr(
      'runtime.ai.chat.stopped', {}, '已停止当前 AI 回复；集群作业不受影响',
      'The current AI response was stopped; cluster jobs are unaffected'));
    else VCS.toast(tr('runtime.ai.chat.nothing_to_stop', {},
      '当前没有正在生成的 AI 回复', 'No AI response is currently being generated'));
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
    if (r && r.error) {
      VCS.log(tr('runtime.ai.source.pick_pdf_failed', { error: r.error },
        '选择 PDF 失败:{error}', 'Failed to select PDF: {error}'), 'failc');
      return;
    }
    if (r && r.path && $('ai-pdf')) $('ai-pdf').value = r.path;
  }

  async function extract() {
    const pdf = ($('ai-pdf') && $('ai-pdf').value.trim()) || '';
    const text = ($('ai-text') && $('ai-text').value.trim()) || '';
    const source = pdf || text;
    if (!source) {
      VCS.log(tr('runtime.ai.extract.source_required', {},
        '请提供 PDF 路径或粘贴方法学文本',
        'Provide a PDF path or paste the methodology text'), 'failc');
      return;
    }
    const btn = $('ai-extract-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.ai.extract.running', {},
      '提取规格表(LLM 只抽文本,每格带出处)…',
      'Extracting the specification (the LLM only extracts text and cites every field)…'));
    try {
      const r = await VCS.call('ai_extract', source);
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.ai.extract.failed', {
          error: (r && r.error) || tr(
            'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '提取失败:{error}', 'Extraction failed: {error}'), 'failc');
        return;
      }
      State.spec = r.spec || {};
      renderSpec();
      (r.issues || []).forEach(i => VCS.log(tr(
        'runtime.ai.extract.validation_note', { issue: i },
        '校验提示:{issue}', 'Validation note: {issue}'), 'warnc'));
      VCS.log(tr('runtime.ai.extract.complete', {},
        '规格表已生成,可逐格编辑后「生成计划」',
        'The specification is ready; edit individual fields, then select Generate plan'), 'okc');
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
    h += group(tr('runtime.ai.spec.system', {}, '体系', 'System'), [
      cellRow('substrate', tr('runtime.ai.spec.substrate', {}, '基底', 'Substrate'),
        leafVal(sys.substrate), leafOrigin(sys.substrate), isLow(sys.substrate)),
      cellRow('metals', tr('runtime.ai.spec.metals', {}, '金属(逗号分隔)',
        'Metals (comma-separated)'), leafList(sys.metals), '', false),
      cellRow('sites', tr('runtime.ai.spec.sites', {}, '配位模板(逗号分隔)',
        'Coordination templates (comma-separated)'), leafList(sys.sites), '', false),
      cellRow('adsorbates', tr('runtime.ai.spec.adsorbates', {}, '吸附质(逗号分隔)',
        'Adsorbates (comma-separated)'), leafList(spec.adsorbates), '', false),
    ]);
    // 方法
    const methods = spec.methods || {};
    h += group(tr('runtime.ai.spec.methods', {}, '方法', 'Methods'), METHOD_FIELDS.map(f =>
      cellRow('m_' + f, f, leafVal(methods[f]), leafOrigin(methods[f]), isLow(methods[f]))));
    // 反应
    h += group(tr('runtime.ai.spec.reactions', {}, '反应', 'Reactions'), [
      cellRow('reactions', tr('runtime.ai.spec.reaction_presets', {},
        '反应预设(逗号分隔)', 'Reaction presets (comma-separated)'),
      leafList(spec.reactions), '', false),
    ]);
    // 产出
    h += group(tr('runtime.ai.spec.outputs', {}, '产出', 'Outputs'), [
      cellRow('outputs', tr('runtime.ai.spec.chart_types', {}, '图表类型(逗号分隔)',
        'Chart types (comma-separated)'), leafList(spec.outputs), '', false),
      cellRow('mode', tr('runtime.ai.spec.mode', {}, '模式(reproduce/rebuild/design)',
        'Mode (reproduce/rebuild/design)'), leafVal(spec.mode), leafOrigin(spec.mode), isLow(spec.mode)),
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
      (low ? `<span class="ai-lowconf">${VCS.esc(tr(
        'runtime.ai.spec.low_confidence', {}, '低置信', 'Low confidence',
      ))}</span>` : '') +
      (origin ? `<span class="ai-origin" title="${VCS.esc(origin)}">${VCS.esc(tr(
        'runtime.ai.spec.source', {}, '出处', 'Source',
      ))}</span>` : '') +
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
    VCS.log(tr('runtime.ai.plan.running', {},
      '生成实例化计划(纯计划对象,不落盘)…',
      'Generating an instantiation plan (plan object only; nothing is written to disk)…'));
    try {
      const r = await VCS.call('ai_plan', buildSpec());
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.ai.plan.failed', {
          error: (r && r.error) || tr(
            'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '生成计划失败:{error}', 'Plan generation failed: {error}'), 'failc');
        return;
      }
      State.plan = r.plan;
      State.planResult = r;
      renderPlan(r);
      VCS.log(tr('runtime.ai.plan.complete', { count: r.jobs_estimate || 0 },
        '计划已生成:约 {count} 个作业',
        'Plan generated: approximately {count} jobs'), 'okc');
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
      `<span>${VCS.esc(tr('runtime.ai.plan.matrix_size', {
        metals: nMetals, templates: nTemplates,
      }, '矩阵规模:{metals} 金属 × {templates} 模板',
      'Matrix size: {metals} metals × {templates} templates'))}</span>` +
      `<span>${VCS.esc(tr('runtime.ai.plan.job_count', {
        count: r.jobs_estimate || 0,
      }, '作业数:{count}', 'Jobs: {count}'))}</span>` +
      `<span>${VCS.esc(tr('runtime.ai.plan.core_hours', {
        hours: r.est_core_hours == null ? '—' : r.est_core_hours,
      }, '预估机时:{hours} 核时', 'Estimated compute time: {hours} core-hours'))}</span>` +
      `<span>${VCS.esc(tr('runtime.ai.plan.pilot', {
        pilot: plan.pilot || '—',
      }, '单点先行:{pilot}', 'Pilot first: {pilot}'))}</span></div>`;
    (r.warnings || []).forEach(w => {
      h += `<div class="sub" style="color:var(--warn);margin:4px 0">⚠ ${VCS.esc(w)}</div>`;
    });
    box.innerHTML = h;
    if (card) card.hidden = false;
  }

  // ── 步骤 3b:实例化(单点先行)+ 全自动模式开关 ──
  async function pickOutRoot() {
    const r = await VCS.call('pick_dir');
    if (r && r.error) {
      VCS.log(tr('runtime.ai.instantiate.pick_directory_failed', { error: r.error },
        '选择目录失败:{error}', 'Failed to select directory: {error}'), 'failc');
      return;
    }
    if (r && r.path && $('ai-outroot')) $('ai-outroot').value = r.path;
  }

  async function instantiate() {
    if (!State.plan) {
      VCS.log(tr('runtime.ai.instantiate.plan_required', {},
        '请先生成计划', 'Generate a plan first'), 'failc');
      return;
    }
    const root = ($('ai-outroot') && $('ai-outroot').value.trim()) || '';
    if (!root) {
      VCS.log(tr('runtime.ai.instantiate.output_required', {},
        '请选择实例化输出根目录', 'Select an output root for instantiation'), 'failc');
      return;
    }
    const auto = $('ai-autopilot') && $('ai-autopilot').checked;
    const msg = auto
      ? tr('runtime.ai.instantiate.confirm_autopilot', {},
        '全自动模式:仍受机时上限 + 单点先行 + 三态门禁约束,且需已在设置页允许外发。继续实例化?',
        'Autopilot remains constrained by the compute-time limit, pilot-first release, and three-state gate, and external access must be enabled in Settings. Continue?')
      : tr('runtime.ai.instantiate.confirm', {},
        '将实例化计划(先释放代表作业,单点先行)。继续?',
        'Instantiate the plan, releasing the representative pilot job first?');
    if (!await VCS.confirm(msg)) return;
    const btn = $('ai-inst-btn');
    if (btn) btn.disabled = true;
    VCS.log(tr('runtime.ai.instantiate.running', {},
      '实例化中(门禁:机时闸 → 单点先行 → 写 campaign → 账本)…',
      'Instantiating (compute-time gate → pilot-first release → campaign write → ledger)…'));
    try {
      const r = await VCS.call('ai_instantiate', State.plan, root, {});
      if (!r || r.ok === false || r.error) {
        VCS.log(tr('runtime.ai.instantiate.failed', {
          error: (r && r.error) || tr(
            'runtime.ai.common.unknown_error', {}, '未知错误', 'Unknown error'),
        }, '实例化失败:{error}', 'Instantiation failed: {error}'), 'failc');
        return;
      }
      VCS.log(tr('runtime.ai.instantiate.created', { count: (r.created || []).length },
        '已实例化 {count} 个作业节点', 'Instantiated {count} job nodes'), 'okc');
      if (r.campaign_dir) VCS.log('campaign:' + r.campaign_dir, 'okc');
      if (r.awaiting === 'pilot_validation') {
        VCS.log(tr('runtime.ai.instantiate.pilot_released', {
          job: (r.pilot && r.pilot.id) || '',
        }, '单点先行:代表作业 {job} 先跑,验证通过后再放行 fan-out',
        'Pilot first: representative job {job} runs before fan-out is released after validation'), 'okc');
      }
      VCS.toast(tr('runtime.ai.instantiate.complete', {},
        '已实例化(单点先行)', 'Instantiated with pilot-first release'));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 能力一:数据对照(提取文献数据表 + 与项目计算值对照) ──
  async function extractTables() {
    const source = (($('ai-pdf') && $('ai-pdf').value.trim()) || '') ||
      (($('ai-text') && $('ai-text').value.trim()) || '');
    if (!source) {
      VCS.log(tr('runtime.ai.tables.source_required', {},
        '请先提供 PDF 路径或粘贴论文文本',
        'Provide a PDF path or paste the paper text first'), 'failc');
      return;
    }
    VCS.log(tr('runtime.ai.tables.extracting', {},
      '提取文献数据表(LLM 只誊抄印着的数值 + 确定性校验)…',
      'Extracting literature data tables (the LLM only transcribes printed values, followed by deterministic validation)…'));
    const r = await VCS.call('ai_extract_tables', source);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.tables.extract_failed', {
        error: (r && r.error) || tr('runtime.ai.common.unknown', {}, '未知', 'Unknown'),
      }, '提取数据表失败:{error}', 'Data-table extraction failed: {error}'), 'failc');
      return;
    }
    State.tables = r.tables || [];
    renderTables();
    VCS.log(tr('runtime.ai.tables.extracted', { count: State.tables.length },
      '已提取 {count} 张数据表(仅供对照,绝不回流计算)',
      'Extracted {count} data tables for comparison only; they never feed back into calculations'), 'okc');
  }
  function renderTables() {
    const box = $('ai-tables');
    if (!box) return;
    if (!State.tables || !State.tables.length) {
      box.innerHTML = `<span class="sub">${VCS.esc(tr(
        'runtime.ai.tables.empty', {}, '未提取到数据表', 'No data tables were extracted',
      ))}</span>`;
      return;
    }
    let h = '';
    State.tables.forEach(t => {
      h += `<div class="sub" style="margin:6px 0 2px"><b>${VCS.esc(t.label || tr(
        'runtime.ai.tables.default_label', {}, '表', 'Table',
      ))}</b> · ${VCS.esc(t.kind || '')}</div>`;
      h += `<table class="ai-tbl"><thead><tr><th>${VCS.esc(tr(
        'runtime.ai.tables.system', {}, '体系', 'System',
      ))}</th><th>${VCS.esc(tr('runtime.ai.tables.species', {}, '物种', 'Species'))}</th>` +
        `<th>${VCS.esc(tr('runtime.ai.tables.literature_value', {}, '文献值/eV', 'Literature value/eV'))}</th>` +
        `<th>${VCS.esc(tr('runtime.ai.tables.page', {}, '页码', 'Page'))}</th></tr></thead><tbody>`;
      (t.rows || []).forEach(r => {
        h += `<tr><td>${VCS.esc(r.system || '')}</td><td>${VCS.esc(r.species || '')}</td>` +
          `<td class="num">${r.value_ev == null ? '—' : r.value_ev}</td>` +
          `<td class="num" title="${VCS.esc(tr(
            'runtime.ai.tables.page', {}, '页码', 'Page',
          ))}">${r.page_hint == null ? '—' : r.page_hint}</td></tr>`;
      });
      h += '</tbody></table>';
    });
    box.innerHTML = h;
  }
  async function compareLit() {
    const proj = ($('ai-cmp-project') && $('ai-cmp-project').value) || '';
    if (!proj) {
      VCS.log(tr('runtime.ai.compare.project_required', {},
        '请选对照项目', 'Select a comparison project'), 'failc');
      return;
    }
    if (!State.tables || !State.tables.length) {
      VCS.log(tr('runtime.ai.compare.tables_required', {},
        '请先提取文献数据表', 'Extract the literature data tables first'), 'failc');
      return;
    }
    VCS.log(tr('runtime.ai.compare.running', {},
      '与文献对照(体系×物种×量对齐,纯确定性)…',
      'Comparing with the literature using deterministic system × species × quantity alignment)…'));
    const r = await VCS.call('ai_compare', proj, State.tables);
    const out = $('ai-compare-out');
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.compare.failed', {
        error: (r && r.error) || tr('runtime.ai.common.unknown', {}, '未知', 'Unknown'),
      }, '对照失败:{error}', 'Comparison failed: {error}'), 'failc');
      return;
    }
    State.compareProj = proj;
    let h = `<div class="ai-metrics"><span>${VCS.esc(tr(
      'runtime.ai.compare.items', { count: r.n }, '比对项 {count}', 'Compared items: {count}',
    ))}</span>` +
      `<span>MAE <b>${r.mae == null ? '—' : (+r.mae).toFixed(3)}</b> eV</span>` +
      `<span>RMSE <b>${r.rmse == null ? '—' : (+r.rmse).toFixed(3)}</b> eV</span></div>`;
    if ((r.worst || []).length) h += `<div class="ai-worst sub">${VCS.esc(tr(
      'runtime.ai.compare.largest_deviation', {}, '偏差最大:', 'Largest deviations:',
    ))}` +
      r.worst.map(w => `${VCS.esc(w.system)}/${VCS.esc(w.species)}(Δ${(+w.delta).toFixed(2)})`)
        .join(VCS.i18n && VCS.i18n.lang === 'en' ? ', ' : '、') + '</div>';
    h += `<table class="ai-tbl"><thead><tr><th>${VCS.esc(tr(
      'runtime.ai.tables.system', {}, '体系', 'System',
    ))}</th><th>${VCS.esc(tr('runtime.ai.tables.species', {}, '物种', 'Species'))}</th>` +
      `<th>${VCS.esc(tr('runtime.ai.compare.literature', {}, '文献/eV', 'Literature/eV'))}</th>` +
      `<th>${VCS.esc(tr('runtime.ai.compare.computed', {}, '计算/eV', 'Computed/eV'))}</th>` +
      `<th>${VCS.esc(tr('runtime.ai.compare.difference', {}, '差/eV', 'Difference/eV'))}</th></tr></thead><tbody>`;
    (r.pairs || []).forEach(p => {
      h += `<tr><td>${VCS.esc(p.system)}</td><td>${VCS.esc(p.species)}</td>` +
        `<td class="num">${(+p.ref).toFixed(3)}</td><td class="num">${(+p.ours).toFixed(3)}</td>` +
        `<td class="num">${(+p.delta).toFixed(3)}</td></tr>`;
    });
    h += '</tbody></table><div class="sub" style="margin-top:6px">' + VCS.esc(r.summary || '') + '</div>';
    h += `<div class="actions" style="margin-top:8px"><button class="btn" id="ai-write-val">${VCS.esc(tr(
      'runtime.ai.compare.write_validation', {}, '写入 validation.md', 'Write validation.md',
    ))}</button></div>`;
    if (out) out.innerHTML = h;
    const wb = $('ai-write-val');
    if (wb) wb.addEventListener('click', writeValidation);
    VCS.log(tr('runtime.ai.compare.complete', { summary: r.summary || '' },
      '对照完成:{summary}', 'Comparison complete: {summary}'), 'okc');
  }
  async function writeValidation() {
    if (!State.compareProj || !State.tables) return;
    const r = await VCS.call('ai_write_validation', State.compareProj, State.tables);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.compare.write_failed', {
        error: (r && r.error) || tr('runtime.ai.common.unknown', {}, '未知', 'Unknown'),
      }, '写入 validation.md 失败:{error}', 'Failed to write validation.md: {error}'), 'failc');
      return;
    }
    VCS.log(tr('runtime.ai.compare.written_path', { path: r.path },
      '已写入 {path}', 'Written to {path}'), 'okc');
    VCS.toast(tr('runtime.ai.compare.written', {},
      'validation.md 已写入', 'validation.md was written'));
  }

  // ── 能力二:材料变体 ──
  async function suggestVariants() {
    VCS.log(tr('runtime.ai.variants.running', {},
      '推荐材料变体(母版邻域 · 排除已算组合)…',
      'Recommending material variants near the parent design while excluding completed combinations)…'));
    const r = await VCS.call('ai_variants', buildSpec(), null);
    const out = $('ai-variants-out');
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.variants.failed', {
        error: (r && r.error) || tr('runtime.ai.common.unknown', {}, '未知', 'Unknown'),
      }, '推荐变体失败:{error}', 'Variant recommendation failed: {error}'), 'failc');
      return;
    }
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
      h += `<div class="sub">${VCS.esc(tr('runtime.ai.variants.batch', {
        batch: b.batch, reason: b.reason || '', jobs: (b.jobs || []).join(', '),
        hours: b.estimate_hours,
      }, '批 {batch}({reason}):{jobs} · ~{hours} 核时',
      'Batch {batch} ({reason}): {jobs} · ~{hours} core-hours'))}</div>`;
    });
    if (out) out.innerHTML = h;
    const gen = $('ai-variants-gen'); if (gen) gen.hidden = !(r.variants || []).length;
    VCS.log(tr('runtime.ai.variants.complete', { count: (r.variants || []).length },
      '已推荐 {count} 个变体', 'Recommended {count} variants'), 'okc');
  }
  async function genVariantMatrix() {
    if (!State.variants || !State.variants.matrix_spec) return;
    const ms = State.variants.matrix_spec;
    if (!await VCS.confirm(tr('runtime.ai.variants.confirm_transfer', {
      metals: (ms.metals || []).join(','), templates: (ms.templates || []).join(','),
    }, '将把变体矩阵({metals} × {templates})带入①结构建模的 SAC 批量建模,请补 INCAR/输出根后生成。继续?',
    'Transfer the variant matrix ({metals} × {templates}) to SAC batch modeling in Structure Modeling? Add INCAR and an output root before generation.'))) return;
    const more = document.getElementById('sac-metal-more');
    if (more) more.value = (ms.metals || []).join(', ');
    const nav = document.querySelector('nav a[data-page="structure"]');
    if (nav) nav.click();
    VCS.toast(tr('runtime.ai.variants.transferred', {},
      '已带入 SAC 批量建模(补 INCAR/输出根后生成矩阵)',
      'Transferred to SAC batch modeling; add INCAR and an output root to generate the matrix'));
    VCS.log(tr('runtime.ai.variants.transferred_metals', {
      metals: (ms.metals || []).join(', '),
    }, '变体矩阵已带入 SAC 建模:金属 {metals}',
    'Variant matrix transferred to SAC modeling; metals: {metals}'), 'okc');
  }

  // ── 能力三:论文草稿 ──
  async function genManuscript() {
    const proj = ($('ai-ms-project') && $('ai-ms-project').value) || '';
    if (!proj) {
      VCS.log(tr('runtime.ai.manuscript.project_required', {},
        '请选项目', 'Select a project'), 'failc');
      return;
    }
    const fmt = ($('ai-ms-fmt') && $('ai-ms-fmt').value) || 'markdown';
    VCS.log(tr('runtime.ai.manuscript.running', {},
      '生成论文骨架(Methods 全自动 · Results 逐图数据句)…',
      'Generating the manuscript scaffold (automatic Methods; figure-specific data statements for Results)…'));
    const r = await VCS.call('ai_manuscript', proj, fmt);
    const out = $('ai-ms-out');
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.ai.manuscript.failed', {
        error: (r && r.error) || tr('runtime.ai.common.unknown', {}, '未知', 'Unknown'),
      }, '生成论文骨架失败:{error}', 'Manuscript scaffold generation failed: {error}'), 'failc');
      return;
    }
    const st = r.stats || {};
    let h = '<div style="margin:6px 0">' +
      `<span class="ai-stat-pill">${VCS.esc(tr('runtime.ai.manuscript.automatic_sentences', {
        count: st.auto == null ? '—' : st.auto,
      }, '自动句 {count}', 'Automatic sentences: {count}'))}</span>` +
      `<span class="ai-stat-pill">${VCS.esc(tr('runtime.ai.manuscript.placeholders', {
        count: st.placeholder == null ? '—' : st.placeholder,
      }, '占位待补 {count}', 'Placeholders to complete: {count}'))}</span>` +
      `<span class="ai-stat-pill">${VCS.esc(tr('runtime.ai.manuscript.automatic_rate', {
        rate: st.auto_ratio == null ? '—' : Math.round(st.auto_ratio * 100) + '%',
      }, '自动率 {rate}', 'Automatic rate: {rate}'))}</span></div>`;
    h += `<div class="sub">${VCS.esc(tr('runtime.ai.manuscript.sections', {
      sections: (r.sections || []).join(' · '),
    }, '章节:{sections}', 'Sections: {sections}'))}</div>`;
    if (!r.docx_available && fmt === 'docx') h += `<div class="sub" style="color:var(--warn)">${VCS.esc(
      r.note || tr('runtime.ai.manuscript.docx_unavailable', {},
        '未安装 python-docx,已降级只出 .md',
        'python-docx is not installed; output was downgraded to .md only'))}</div>`;
    h += `<div class="actions" style="margin-top:8px"><button class="btn" data-open="${VCS.esc(r.path || '')}">${VCS.esc(tr(
      'runtime.ai.manuscript.open_output', {}, '打开产物', 'Open output',
    ))}</button></div>`;
    if (out) out.innerHTML = h;
    VCS.log(tr('runtime.ai.manuscript.complete', {
      automatic: st.auto || 0, placeholders: st.placeholder || 0,
    }, '论文骨架已生成:自动 {automatic} 句 / 占位 {placeholders} 处(诚实呈现)',
    'Manuscript scaffold generated: {automatic} automatic sentences / {placeholders} explicit placeholders'), 'okc');
  }

  async function loadProjects() {
    const r = await VCS.call('proj_list');
    const projs = (r && r.projects) || [];
    const opts = `<option value="">${VCS.esc(tr(
      'runtime.ai.projects.select', {}, '(选项目)', '(Select project)',
    ))}</option>` +
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
    document.addEventListener('vcs:language', () => {
      renderChat();
      if (State.spec) renderSpec();
      if (State.planResult) renderPlan(State.planResult);
      if (State.tables) renderTables();
    });
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
