// methods.js — C3 Methods 段生成。扩展全局 VCS,提供 showMethods()。
// 依赖:app.js(VCS.modal/call/esc)。纯本地,无外部依赖。
(function () {
  'use strict';

  // 复制到剪贴板:clipboard API 失败(pywebview 权限差异)降级为选中文本让用户 Ctrl+C
  function copyText(text, pre, btn) {
    const done = function (ok) {
      const old = btn.textContent;
      btn.textContent = ok ? '已复制' : '已选中,请 Ctrl+C';
      setTimeout(function () { btn.textContent = old; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { done(true); },
        function () { selectPre(pre); done(false); });
    } else {
      selectPre(pre);
      done(false);
    }
  }

  function selectPre(pre) {
    const r = document.createRange();
    r.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
  }

  function section(wrap, label, text) {
    const h = document.createElement('div');
    h.className = 'meth-h';
    const btn = document.createElement('button');
    btn.className = 'btn quiet';
    btn.textContent = '复制';
    const cap = document.createElement('span');
    cap.textContent = label;
    h.appendChild(cap);
    h.appendChild(btn);
    const pre = document.createElement('pre');
    pre.className = 'mono meth-pre';
    pre.textContent = text || '(无内容)';
    btn.addEventListener('click', function () { copyText(text || '', pre, btn); });
    wrap.appendChild(h);
    wrap.appendChild(pre);
  }

  // 打开某作业的 Methods 段模态(中文/English/BibTeX 三段 + 各自复制)。
  VCS.showMethods = async function (jobDir, name) {
    const wrap = document.createElement('div');
    const note = document.createElement('div');
    note.className = 'struct-note';
    note.hidden = true;
    wrap.appendChild(note);
    const body = document.createElement('div');
    body.textContent = '生成中…';
    wrap.appendChild(body);

    const m = VCS.modal({
      title: '计算方法段 — ' + name,
      body: wrap,
      actions: [{ label: '关闭', quiet: true, onClick: h => h.close() }],
    });
    m.el.classList.add('modal-wide');

    const out = await VCS.call('methods_text', jobDir);
    body.textContent = '';
    if (!out || out.error || !out.ok) {
      note.hidden = false;
      note.className = 'struct-note warn-banner';
      note.textContent = (out && out.error) || '生成方法段失败';
      return;
    }
    if (out.warnings && out.warnings.length) {
      note.hidden = false;
      note.className = 'struct-note warn-banner';
      note.textContent = out.warnings.join(';');
    }
    section(body, '中文', out.zh);
    section(body, 'English', out.en);
    section(body, 'BibTeX', out.bibtex);
  };
})();
