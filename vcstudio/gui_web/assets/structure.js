// structure.js — C2 结构 3D 预览。扩展全局 VCS,提供 showStructure()。
// 依赖:app.js(VCS.modal/call/esc)、vendor/3Dmol-min.js。离线,无 CDN。
(function () {
  'use strict';

  // xyz 文本第 2+i 行 → 该原子笛卡尔坐标(与后端 structure_view 输出一一对应)
  function atomXYZ(xyzText, i) {
    const parts = xyzText.split('\n')[2 + i].trim().split(/\s+/);
    return { x: +parts[1], y: +parts[2], z: +parts[3] };
  }

  function gapSummary(gap) {
    if (!gap) return '';
    if (gap.separated) {
      let s = `分子-衬底最近 ${gap.min_dist} Å(垂直间隙 ${gap.vertical_gap} Å`;
      if (gap.mol_formula) s += `,分子 ${gap.mol_formula}`;
      return s + ')';
    }
    if (gap.min_dist != null) return `最近原子对 ${gap.min_dist} Å`;
    return '';
  }

  // 打开结构 3D 预览模态。filename:null=path 即文件;'AUTO'=job_dir 内 CONTCAR→POSCAR。
  VCS.showStructure = async function (path, filename, title) {
    const wrap = document.createElement('div');
    const info = document.createElement('div');
    info.className = 'struct-info';
    info.textContent = '加载中…';
    const note = document.createElement('div');
    note.className = 'struct-note';
    note.hidden = true;
    const box = document.createElement('div');
    box.className = 'struct-canvas';
    wrap.appendChild(info);
    wrap.appendChild(note);
    wrap.appendChild(box);

    let viewer = null;
    let closed = false;
    const onResize = function () {
      if (viewer) { try { viewer.resize(); viewer.render(); } catch (e) { /* 忽略 */ } }
    };
    const m = VCS.modal({
      title: '结构预览 — ' + title,
      body: wrap,
      actions: [{ label: '关闭', quiet: true, onClick: h => h.close() }],
    });
    m.el.classList.add('modal-wide');
    const closeInner = m.close;
    m.close = function () {
      closed = true;
      window.removeEventListener('resize', onResize);
      if (viewer) { try { viewer.clear(); } catch (e) { /* 无损清理 */ } viewer = null; }
      closeInner();
    };

    const out = await VCS.call('struct_view', path, filename);
    if (closed) return;   // 加载期间已关闭:放弃渲染
    if (!out || out.error || !out.ok) {
      info.textContent = '';
      note.hidden = false;
      note.className = 'struct-note warn-banner';
      note.textContent = (out && out.error) || '读取结构失败';
      return;
    }
    const v = out.view;
    const gap = v.gap || {};
    info.textContent = `${v.formula} · ${v.natoms} 原子 · 文件 ${out.used}` +
      (gapSummary(gap) ? ' · ' + gapSummary(gap) : '');

    // 间隙告警条:crash 红字 / warn 黄条;后端 notes 原样列出
    const msgs = [].concat(gap.notes || [], v.notes || []);
    if (msgs.length) {
      note.hidden = false;
      note.className = 'struct-note warn-banner' + (gap.level === 'crash' ? ' crash' : '');
      note.textContent = msgs.join(';');
    }

    if (typeof window.$3Dmol === 'undefined') {
      note.hidden = false;
      note.className = 'struct-note warn-banner';
      note.textContent = '3D 库未加载(3Dmol 缺失)';
      return;
    }
    try {
      viewer = window.$3Dmol.createViewer(box, { backgroundColor: '#FFFFFF' });
      viewer.addModel(v.xyz, 'xyz');
      if (v.natoms > 5000) {
        viewer.setStyle({}, { stick: { radius: 0.12 } });   // 大体系降级仅棍状
      } else {
        viewer.setStyle({}, { sphere: { scale: 0.3 }, stick: { radius: 0.15 } });
      }
      // 标注对:crash 时优先画真正的重叠对(gap.clash),否则画分子-衬底最近对。
      // 仅当胞内直线距离≈报告值时画虚线(跨周期像的近邻画进胞内会误导)。
      const mark = (gap.clash && gap.clash.dist != null)
        ? { i: gap.clash.i, j: gap.clash.j, dist: gap.clash.dist }
        : ((gap.pair && gap.min_dist != null)
            ? { i: gap.pair.i, j: gap.pair.j, dist: gap.min_dist } : null);
      if (mark) {
        const p = atomXYZ(v.xyz, mark.i), q = atomXYZ(v.xyz, mark.j);
        const d = Math.sqrt((p.x - q.x) ** 2 + (p.y - q.y) ** 2 + (p.z - q.z) ** 2);
        if (Math.abs(d - mark.dist) < 1e-3) {
          const color = gap.level === 'crash' ? '#BB4444' : '#B8952E';
          viewer.addCylinder({ start: p, end: q, radius: 0.05, dashed: true,
                               color: color, fromCap: 1, toCap: 1 });
          viewer.addLabel(`${mark.dist} Å`, {
            position: { x: (p.x + q.x) / 2, y: (p.y + q.y) / 2, z: (p.z + q.z) / 2 },
            backgroundColor: color, backgroundOpacity: 0.85,
            fontColor: '#FFFFFF', fontSize: 12, borderRadius: 4,
          });
        }
      }
      viewer.zoomTo();
      viewer.render();
      window.addEventListener('resize', onResize);
      // 3Dmol 可能在布局稳定前测量容器(得到 0 宽),入场后强制重排一次
      setTimeout(onResize, 30);
    } catch (e) {
      note.hidden = false;
      note.className = 'struct-note warn-banner';
      note.textContent = '3D 渲染失败(当前环境可能不支持 WebGL):' + (e && e.message ? e.message : e);
    }
  };
})();
