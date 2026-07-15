// dos.js — C4 DOS 出图。扩展全局 VCS,提供 showDos()。
// SVG 由后端 charts.render_dos_svg 生成(受控自产内容),modal 内直接嵌入。
(function () {
  'use strict';

  VCS.showDos = async function (jobDir, name) {
    const wrap = document.createElement('div');
    const note = document.createElement('div');
    note.className = 'struct-note';
    note.hidden = true;
    const box = document.createElement('div');
    box.className = 'dos-box';
    box.textContent = '解析 vasprun.xml…';
    wrap.appendChild(note);
    wrap.appendChild(box);

    const m = VCS.modal({
      title: 'DOS — ' + name,
      body: wrap,
      actions: [{ label: '关闭', quiet: true, onClick: h => h.close() }],
    });
    m.el.classList.add('modal-wide');

    const out = await VCS.call('dos_view', jobDir);
    if (!out || out.error || !out.ok) {
      box.textContent = '';
      note.hidden = false;
      note.className = 'struct-note warn-banner';
      note.textContent = (out && out.error) || '生成 DOS 图失败';
      return;
    }
    box.innerHTML = out.svg;   // 后端自产 SVG,受控内容
    const msgs = [].concat(out.warnings || []);
    if (out.saved) msgs.unshift('已保存:' + out.saved);
    if (msgs.length) {
      note.hidden = false;
      note.className = 'struct-note' + (out.warnings && out.warnings.length ? ' warn-banner' : '');
      note.textContent = msgs.join(';');
    }
  };
})();
