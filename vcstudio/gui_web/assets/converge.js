// converge.js — C1 收敛过程可视化。扩展全局 VCS,提供 showConvergence()。
// 依赖:app.js(VCS.modal/call/esc)、vendor/echarts.min.js。离线,无 CDN。
(function () {
  'use strict';

  // 仅取正值用于 log 轴(0/负/null → 断点);dE 首步与缺失 |F|max 天然为 null。
  function posLog(v) { return (v != null && v > 0) ? v : null; }

  // 打开某作业的收敛曲线模态:E0(左轴)+ |ΔE|、|F|max(右 log 轴)vs 离子步。
  VCS.showConvergence = async function (jobDir, name) {
    const wrap = document.createElement('div');
    const note = document.createElement('div');
    note.className = 'conv-note';
    note.textContent = '加载中…';
    const chartEl = document.createElement('div');
    chartEl.className = 'conv-chart';
    wrap.appendChild(note);
    wrap.appendChild(chartEl);

    let chart = null;
    const onResize = function () { if (chart) chart.resize(); };

    const m = VCS.modal({
      title: '收敛过程 — ' + name,
      body: wrap,
      actions: [{ label: '关闭', quiet: true, onClick: h => h.close() }],
    });
    m.el.classList.add('modal-wide');   // 图表需更宽的模态(默认 440px 太窄)
    // 关闭/取消(遮罩/Esc 都走 handle.close)时释放图表与监听,防泄漏。
    const closeInner = m.close;
    m.close = function () {
      window.removeEventListener('resize', onResize);
      if (chart) { chart.dispose(); chart = null; }
      closeInner();
    };

    const out = await VCS.call('conv_series', jobDir);
    if (!out || out.error || !out.ok) {
      note.className = 'conv-note warn-banner';
      note.textContent = (out && out.error) || '读取收敛数据失败';
      return;
    }
    const s = out.series || {};
    if (!s.steps || !s.steps.length) {
      note.className = 'conv-note warn-banner';
      note.textContent = (s.notes && s.notes.length) ? s.notes.join(';') : '无离子步数据';
      return;
    }
    if (typeof window.echarts === 'undefined') {
      note.className = 'conv-note warn-banner';
      note.textContent = '图表库未加载(echarts 缺失)';
      return;
    }

    // 顶部说明:有 notes(缺力/数不一致等)高亮黄条,否则简述步数。
    if (s.notes && s.notes.length) {
      note.className = 'conv-note warn-banner';
      note.textContent = s.notes.join(';');
    } else {
      note.className = 'conv-note';
      note.textContent = s.steps.length + ' 个离子步';
    }

    chart = window.echarts.init(chartEl);
    chart.setOption({
      color: ['#4477AA', '#CC6677', '#228833'],
      tooltip: { trigger: 'axis' },
      legend: { data: ['E0 (eV)', '|ΔE| (eV)', '|F|max (eV/Å)'], bottom: 0 },
      grid: { left: 66, right: 66, top: 24, bottom: 44 },
      xAxis: { type: 'category', name: '离子步', nameLocation: 'middle',
        nameGap: 26, data: s.steps },
      yAxis: [
        { type: 'value', name: 'E0 (eV)', scale: true },
        { type: 'log', name: '|ΔE| / |F|max', position: 'right' },
      ],
      series: [
        { name: 'E0 (eV)', type: 'line', yAxisIndex: 0, symbol: 'circle',
          symbolSize: 5, data: s.E0 },
        { name: '|ΔE| (eV)', type: 'line', yAxisIndex: 1, connectNulls: false,
          symbol: 'circle', symbolSize: 4, data: (s.dE || []).map(posLog) },
        { name: '|F|max (eV/Å)', type: 'line', yAxisIndex: 1, connectNulls: false,
          symbol: 'circle', symbolSize: 4, data: (s.fmax || []).map(posLog) },
      ],
    });
    window.addEventListener('resize', onResize);
    // 模态入场后容器尺寸可能变化,主动重排一次。
    setTimeout(onResize, 30);
  };
})();
