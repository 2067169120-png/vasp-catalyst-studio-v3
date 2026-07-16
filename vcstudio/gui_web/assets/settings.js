// settings.js — 设置页:LLM 智能分析 / 报告提示词 / 数据路径 / 外观与自动化。
// 只依赖 app.js 暴露的 VCS.*(settings_get/llm_*/prompt_*/paths_save/theme_set/autopilot_save)。
// 全部插值走 VCS.esc(textarea/输入框用 .value 天然安全);零 emoji;中文文案。密钥绝不回显。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const val = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const setVal = (id, v) => { const el = $(id); if (el) el.value = (v == null ? '' : v); };
  const setSel = (id, v) => { const el = $(id); if (el) el.value = String(v); };

  // 提供商预设 → [base_url, model](自定义为空,不覆盖用户已填)
  const PROVIDERS = {
    openai: ['https://api.openai.com/v1/chat/completions', 'gpt-4o'],
    deepseek: ['https://api.deepseek.com/v1/chat/completions', 'deepseek-chat'],
    glm: ['https://open.bigmodel.cn/api/paas/v4/chat/completions', 'glm-4-plus'],
    kimi: ['https://api.moonshot.cn/v1/chat/completions', 'moonshot-v1-8k'],
    custom: ['', ''],
  };

  // ── 载入 / 回填 ──────────────────────────────────────────────────────────────
  async function load() {
    const s = await VCS.call('settings_get');
    if (!s || s.ok === false || s.error) {
      VCS.log('读取设置失败:' + ((s && s.error) || '未知错误'), 'failc');
      return;
    }
    const llm = s.llm || {};
    setVal('set-llm-baseurl', llm.base_url);
    setVal('set-llm-model', llm.model);
    if ($('set-llm-external')) $('set-llm-external').checked = !!llm.allow_external;
    renderKeyState(!!llm.key_saved);
    guessProvider(llm.base_url, llm.model);

    const prompt = s.prompt || {};
    setVal('set-prompt', prompt.text);

    const paths = s.paths || {};
    setVal('set-potcar', paths.potcar_lib_root);
    setVal('set-molecules', paths.lis_molecules_dir);
    const iw = paths.ideal_window || [];
    setVal('set-idw-lo', iw.length ? iw[0] : '');
    setVal('set-idw-hi', iw.length > 1 ? iw[1] : '');

    const ui = s.ui || {};
    selectTheme(ui.theme || 'classic', false);
    if ($('set-ap-on')) $('set-ap-on').checked = ui.autopilot !== false;
    setSel('set-ap-interval', ui.poll_interval || 10);
    if ($('set-ap-continue')) $('set-ap-continue').checked = ui.autopilot_continue !== false;
    if ($('set-ap-fetch')) $('set-ap-fetch').checked = ui.autopilot_fetch !== false;
    if ($('set-ap-report')) $('set-ap-report').checked = ui.autopilot_report !== false;
  }

  function renderKeyState(saved) {
    const el = $('set-llm-keystate');
    if (!el) return;
    el.className = 'keystate ' + (saved ? 'saved' : 'unset');
    el.textContent = saved ? '已保存' : '未设置';
  }

  // base_url/model 与某预设完全一致 → 选中该预设,否则「自定义」
  function guessProvider(baseUrl, model) {
    const sel = $('set-llm-provider');
    if (!sel) return;
    let hit = 'custom';
    Object.keys(PROVIDERS).forEach(k => {
      if (k !== 'custom' && PROVIDERS[k][0] === (baseUrl || '') && PROVIDERS[k][1] === (model || '')) hit = k;
    });
    sel.value = hit;
  }

  // ── LLM 卡片 ────────────────────────────────────────────────────────────────
  function onProviderChange() {
    const sel = $('set-llm-provider');
    if (!sel) return;
    const preset = PROVIDERS[sel.value];
    if (!preset || sel.value === 'custom') return;   // 自定义:不动用户已填
    setVal('set-llm-baseurl', preset[0]);
    setVal('set-llm-model', preset[1]);
  }

  async function saveLlm() {
    const r = await VCS.call('llm_save', val('set-llm-baseurl'), val('set-llm-model'),
      $('set-llm-external') ? $('set-llm-external').checked : false);
    if (!(r && r.ok)) { VCS.log('保存 LLM 配置失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    VCS.log('已保存 LLM 端点/模型' + ($('set-llm-external') && $('set-llm-external').checked
      ? '(已允许项目数据外发)' : '(项目数据不外发)'), 'okc');
    VCS.toast('已保存');
  }

  async function saveKey() {
    const key = val('set-llm-key');
    if (!key) { VCS.toast('请先粘贴密钥', 'fail'); return; }
    const r = await VCS.call('llm_key_save', key);
    if (!(r && r.ok)) { VCS.log('保存密钥失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    setVal('set-llm-key', '');            // 存后立即清空输入,绝不回显
    renderKeyState(true);
    VCS.log('API 密钥已存入系统凭据库', 'okc');
    VCS.toast('密钥已保存');
  }

  async function testLlm() {
    const btn = $('set-llm-test');
    if (btn) btn.disabled = true;
    VCS.log('测试 LLM 连接(极小请求)…');
    try {
      const r = await VCS.call('llm_test', val('set-llm-baseurl'), val('set-llm-model'));
      if (r && r.ok) { VCS.log('LLM 连接正常', 'okc'); VCS.toast('连接正常'); }
      else { VCS.log('LLM 连接失败:' + ((r && r.error) || '未知'), 'failc'); VCS.toast('连接失败', 'fail'); }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── 提示词卡片 ──────────────────────────────────────────────────────────────
  async function savePrompt() {
    const t = $('set-prompt') ? $('set-prompt').value : '';
    if (!t.trim()) { VCS.toast('提示词不能为空', 'fail'); return; }
    const r = await VCS.call('prompt_save', t);
    if (!(r && r.ok)) { VCS.log('保存提示词失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    VCS.log('已保存自定义报告分析提示词', 'okc');
    VCS.toast('已保存');
  }

  async function resetPrompt() {
    const ok = await VCS.confirm('恢复内置发刊级默认提示词?(将丢弃当前自定义)');
    if (!ok) return;
    const r = await VCS.call('prompt_reset');
    if (!(r && r.ok)) { VCS.log('恢复默认失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    setVal('set-prompt', r.text);
    VCS.log('已恢复默认提示词', 'okc');
    VCS.toast('已恢复默认');
  }

  // ── 数据路径卡片 ────────────────────────────────────────────────────────────
  async function pickDirInto(id) {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log('选择目录失败:' + r.error, 'failc'); return; }
    if (r && r.path) setVal(id, r.path);
  }

  async function savePaths() {
    const r = await VCS.call('paths_save', val('set-potcar'), val('set-molecules'),
      val('set-idw-lo'), val('set-idw-hi'));
    if (!(r && r.ok)) { VCS.log('保存数据路径失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    VCS.log('已保存数据路径(赝势库 / 分子库 / 理想窗口)', 'okc');
    VCS.toast('已保存');
  }

  // ── 外观与自动化卡片 ────────────────────────────────────────────────────────
  function selectTheme(name, persist) {
    const row = $('set-theme-row');
    if (row) row.querySelectorAll('.theme-opt').forEach(
      o => o.classList.toggle('sel', o.dataset.theme === name));
    if (persist) {
      VCS.themeApply(name);                       // 即时生效 + 记忆(防闪烁)
      VCS.call('theme_set', name).then(() => VCS.log('主题已切换:' + name, 'okc'));
    }
  }

  async function saveAutopilot() {
    const r = await VCS.call('autopilot_save',
      $('set-ap-on') ? $('set-ap-on').checked : true,
      parseInt(val('set-ap-interval'), 10) || 10,
      $('set-ap-continue') ? $('set-ap-continue').checked : true,
      $('set-ap-fetch') ? $('set-ap-fetch').checked : true,
      $('set-ap-report') ? $('set-ap-report').checked : true);
    if (!(r && r.ok)) { VCS.log('保存自动化设置失败:' + ((r && r.error) || '未知'), 'failc'); return; }
    VCS.log('已保存自动驾驶设置', 'okc');
    VCS.toast('已保存');
    if (VCS.pipeline && typeof VCS.pipeline.reconfigure === 'function') VCS.pipeline.reconfigure();
  }

  // ── 初始化 ──────────────────────────────────────────────────────────────────
  function wire(id, ev, fn) { const el = $(id); if (el) el.addEventListener(ev, fn); }

  function init() {
    wire('set-llm-provider', 'change', onProviderChange);
    wire('set-llm-save', 'click', saveLlm);
    wire('set-llm-keysave', 'click', saveKey);
    wire('set-llm-test', 'click', testLlm);
    wire('set-prompt-save', 'click', savePrompt);
    wire('set-prompt-reset', 'click', resetPrompt);
    wire('set-potcar-btn', 'click', () => pickDirInto('set-potcar'));
    wire('set-molecules-btn', 'click', () => pickDirInto('set-molecules'));
    wire('set-paths-save', 'click', savePaths);
    wire('set-ap-save', 'click', saveAutopilot);
    const row = $('set-theme-row');
    if (row) row.addEventListener('click', e => {
      const o = e.target.closest('.theme-opt');
      if (o) selectTheme(o.dataset.theme, true);
    });
    load();
  }

  // 切回设置页时刷新(密钥状态 / 其他会话可能改过 config)
  document.addEventListener('vcs:page', e => {
    if (e.detail && e.detail.page === 'settings') load();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Settings = { reload: load };
})();
