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
    if (!poscar || !incar) {
      State.previewKey = null;
      pre.style.color = '';
      pre.textContent = '(选择 POSCAR / INCAR 后自动解析预览)';
      return;
    }
    const key = poscar + '\n' + incar + '\n' + calc;
    if (key === State.previewKey) return;   // 相同输入不重复解析(镜像 _preview_memo)
    State.previewKey = key;
    pre.style.color = '';
    pre.textContent = '正在解析…';
    const r = await VCS.call('gen_preview', poscar, incar, calc);
    if (State.previewKey !== key) return;   // 期间用户又改了路径 → 丢弃旧响应
    if (!r || r.ok === false || r.error) {
      pre.style.color = 'var(--fail)';
      pre.textContent = '预览失败:' + ((r && r.error) || '未知错误');
      return;
    }
    const s = r.summary || {};
    pre.style.color = '';
    pre.textContent = [s.poscar, s.incar].filter(Boolean).join('\n\n') || '(无预览内容)';
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
    if (btn) btn.disabled = true;
    VCS.log('生成中(' + calc + ')…');
    try {
      const r = await VCS.call('gen_run', poscar, incar, out, lib, calc);
      if (!r || r.ok === false || r.error) {
        VCS.log('生成失败:' + ((r && r.error) || '未知错误'), 'failc');
        return;
      }
      VCS.log('已生成:' + r.job_dir, 'okc');
      (r.warnings || []).forEach(w => VCS.log(w));
      VCS.log('作业已入台账,去任务页提交', 'okc');
      // 同步任务页台账(若已加载)
      if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    } finally {
      if (btn) btn.disabled = false;
    }
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

  window.Generate = { reload: () => refreshPreview() };
})();
