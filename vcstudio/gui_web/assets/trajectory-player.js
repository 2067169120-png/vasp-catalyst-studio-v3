// trajectory-player.js — server-authoritative frame/time navigation and repair review.
(function () {
  'use strict';

  function tr(key, params = {}, zhFallback = '', enFallback = '') {
    const english = !!(VCS.i18n && VCS.i18n.lang === 'en');
    const fallback = english ? (enFallback || zhFallback) : zhFallback;
    if (typeof VCS.t === 'function') return VCS.t(key, params, fallback);
    return String(fallback).replace(/\{([A-Za-z0-9_]+)\}/g,
      (match, name) => Object.prototype.hasOwnProperty.call(params, name)
        ? String(params[name]) : match);
  }

  function number(value, digits = 5) {
    return typeof value === 'number' && Number.isFinite(value)
      ? value.toFixed(digits) : '—';
  }

  function stepLabel(row) {
    return row.image
      ? tr('trajectory.image_label', { image: row.image }, 'image {image}', 'image {image}')
      : tr('trajectory.step_label', { step: row.step }, '第 {step} 步', 'Step {step}');
  }

  function anomalyText(row) {
    const values = {
      crash: tr('trajectory.anomaly.crash', {}, '严重异常', 'Critical anomaly'),
      warn: tr('trajectory.anomaly.warn', {}, '需关注', 'Review'),
      ok: tr('trajectory.anomaly.ok', {}, '未见近距异常', 'No close-contact anomaly'),
      none: tr('trajectory.anomaly.none', {}, '无', 'None'),
      unknown: tr('trajectory.anomaly.unknown', {}, '未知', 'Unknown'),
    };
    return values[row.anomaly] || values.unknown;
  }

  function rowsHtml(page) {
    return (page.rows || []).map(row => {
      const disabled = row.frame_token ? '' : ' disabled';
      const anomaly = anomalyText(row);
      return '<tr>' +
        `<td><button type="button" class="tp-step" data-frame-token="${VCS.esc(row.frame_token || '')}"${disabled}>` +
          `${VCS.esc(stepLabel(row))}</button></td>` +
        `<td>${VCS.esc(number(row.energy_ev, 6))}</td>` +
        `<td>${VCS.esc(number(row.fmax_ev_a, 4))}</td>` +
        `<td>${VCS.esc(number(row.temperature_k, 1))}</td>` +
        `<td>${VCS.esc(number(row.minimum_distance_a, 3))}</td>` +
        `<td><span class="tp-anomaly ${VCS.esc(row.anomaly || 'unknown')}">${VCS.esc(anomaly)}</span></td>` +
        '</tr>';
    }).join('');
  }

  function methodDiffHtml(rows) {
    if (!rows || !rows.length) {
      return `<p class="sub">${VCS.esc(tr('trajectory.repair.no_method_diff', {},
        '可执行恢复保持 INCAR 逐字不变。',
        'The executable recovery keeps INCAR byte-for-byte unchanged.'))}</p>`;
    }
    return '<div class="tp-table-region" role="region" tabindex="0" ' +
      `aria-label="${VCS.esc(tr('trajectory.repair.method_diff', {}, '建议方法差异', 'Suggested method diff'))}">` +
      '<table><thead><tr>' +
      `<th>${VCS.esc(tr('trajectory.repair.key', {}, '键', 'Key'))}</th>` +
      `<th>${VCS.esc(tr('trajectory.repair.before', {}, '当前', 'Current'))}</th>` +
      `<th>${VCS.esc(tr('trajectory.repair.proposed', {}, '建议', 'Suggested'))}</th>` +
      `<th>${VCS.esc(tr('trajectory.repair.execution', {}, '执行边界', 'Execution boundary'))}</th>` +
      '</tr></thead><tbody>' + rows.map(row => '<tr>' +
        `<td>${VCS.esc(row.key || '')}</td><td>${VCS.esc(row.before || '—')}</td>` +
        `<td>${VCS.esc(row.proposed || '')}</td><td>${VCS.esc(row.execution || '')}</td>` +
        '</tr>').join('') + '</tbody></table></div>';
  }

  function buildShell(title) {
    const root = document.createElement('div');
    root.className = 'trajectory-player';
    root.innerHTML =
      `<div class="tp-status" role="status" aria-live="polite">${VCS.esc(tr(
        'trajectory.loading', {}, '正在建立服务端快照…', 'Creating the server snapshot…'))}</div>` +
      '<p class="tp-source sub"></p>' +
      '<div class="tp-grid">' +
        '<section class="tp-structure" aria-labelledby="tp-structure-heading">' +
          `<h3 id="tp-structure-heading">${VCS.esc(tr('trajectory.structure', {}, '结构帧', 'Structure frame'))}</h3>` +
          `<div class="tp-canvas" role="img" aria-label="${VCS.esc(tr(
            'trajectory.structure_canvas', {}, '当前结构三维视图', 'Current 3D structure'))}"></div>` +
          '<div class="tp-frame-note sub" aria-live="polite"></div>' +
        '</section>' +
        '<section class="tp-series" aria-labelledby="tp-series-heading">' +
          `<h3 id="tp-series-heading">${VCS.esc(tr(
            'trajectory.series', {}, '能量 / |F|max / 温度', 'Energy / |F|max / temperature'))}</h3>` +
          `<div class="tp-chart" role="img" aria-label="${VCS.esc(tr(
            'trajectory.chart_label', {}, '服务端解析的轨迹曲线', 'Server-parsed trajectory series'))}"></div>` +
          '<p class="tp-chart-note sub"></p>' +
        '</section>' +
      '</div>' +
      '<section class="tp-steps" aria-labelledby="tp-steps-heading">' +
        '<div class="tp-section-head">' +
          `<h3 id="tp-steps-heading">${VCS.esc(tr('trajectory.steps', {}, '步骤 / image 表', 'Step / image table'))}</h3>` +
          '<label class="sub">' + VCS.esc(tr('trajectory.sampling', {}, 'AIMD 抽样', 'AIMD sampling')) + ' ' +
            '<select class="ipt tp-stride"><option value="1">1×</option><option value="10">10×</option>' +
            '<option value="100">100×</option><option value="1000">1000×</option></select></label>' +
          '<button type="button" class="btn quiet tp-prev">' + VCS.esc(tr(
            'trajectory.previous', {}, '上一页', 'Previous')) + '</button>' +
          '<button type="button" class="btn quiet tp-next">' + VCS.esc(tr(
            'trajectory.next', {}, '下一页', 'Next')) + '</button>' +
        '</div>' +
        `<div class="tp-table-region" role="region" tabindex="0" aria-label="${VCS.esc(tr(
          'trajectory.table_scroll', {}, '轨迹步骤表，可滚动', 'Trajectory step table, scrollable'))}">` +
          '<table><thead><tr>' +
          `<th>${VCS.esc(tr('trajectory.column.step', {}, '步骤', 'Step'))}</th>` +
          '<th>E0 (eV)</th><th>|F|max (eV/Å)</th><th>T (K)</th>' +
          `<th>${VCS.esc(tr('trajectory.column.distance', {}, '关键距离 (Å)', 'Key distance (Å)'))}</th>` +
          `<th>${VCS.esc(tr('trajectory.column.anomaly', {}, '异常', 'Anomaly'))}</th>` +
          '</tr></thead><tbody class="tp-step-body"></tbody></table></div>' +
        '<p class="tp-page-note sub" aria-live="polite"></p>' +
      '</section>' +
      '<section class="tp-repair" aria-labelledby="tp-repair-heading">' +
        `<h3 id="tp-repair-heading">${VCS.esc(tr(
          'trajectory.repair.title', {}, '透明修复审阅（默认只读）', 'Transparent repair review (read-only by default)'))}</h3>` +
        '<div class="tp-repair-body"></div>' +
      '</section>';
    root.dataset.title = String(title || '');
    return root;
  }

  function renderChart(element, overview) {
    const plot = overview.plot || {};
    const points = plot.points || [];
    const note = element.parentNode.querySelector('.tp-chart-note');
    note.textContent = tr('trajectory.chart_sampling_note', {
      count: points.length, stride: plot.sampling_stride || 1,
    }, '曲线最多传输 1000 点；当前 {count} 点，服务端抽样间隔 {stride}。',
    'Charts transfer at most 1,000 points; showing {count} points with server stride {stride}.');
    if (typeof window.echarts === 'undefined') {
      element.textContent = tr('trajectory.chart_unavailable', {},
        '图表引擎不可用；步骤表仍可使用。', 'Chart engine unavailable; the step table remains available.');
      return null;
    }
    const chart = window.echarts.init(element);
    const labels = points.map(point => point.image || point.step);
    chart.setOption({
      animation: false,
      tooltip: { trigger: 'axis' },
      legend: { data: ['E0', '|F|max', 'T'] },
      grid: { left: 56, right: 56, top: 36, bottom: 42 },
      xAxis: { type: 'category', data: labels, name: overview.task_kind === 'neb' ? 'image' : 'step' },
      yAxis: [
        { type: 'value', name: 'eV / eV Å⁻¹', scale: true },
        { type: 'value', name: 'K', scale: true },
      ],
      series: [
        { name: 'E0', type: 'line', showSymbol: points.length < 200,
          data: points.map(point => point.energy_ev), connectNulls: false },
        { name: '|F|max', type: 'line', showSymbol: points.length < 200,
          data: points.map(point => point.fmax_ev_a), connectNulls: false },
        { name: 'T', type: 'line', yAxisIndex: 1, showSymbol: false,
          data: points.map(point => point.temperature_k), connectNulls: false },
      ],
    });
    return chart;
  }

  function renderStructure(element, note, frame) {
    const view = frame && frame.view;
    if (!view || typeof window.$3Dmol === 'undefined') {
      note.textContent = (frame && frame.error) || tr('trajectory.structure_unavailable', {},
        '结构帧不可用。', 'Structure frame unavailable.');
      return null;
    }
    const viewer = window.$3Dmol.createViewer(element, { backgroundColor: '#FFFFFF' });
    viewer.addModel(view.xyz, 'xyz');
    viewer.setStyle({}, view.natoms > 5000
      ? { stick: { radius: 0.12 } }
      : { sphere: { scale: 0.3 }, stick: { radius: 0.15 } });
    viewer.zoomTo(); viewer.render();
    const metric = frame.metrics || {};
    note.textContent = tr('trajectory.frame_summary', {
      formula: view.formula || '', count: view.natoms || 0,
      distance: number(metric.minimum_distance_a, 3), anomaly: metric.anomaly || 'unknown',
    }, '{formula} · {count} 原子 · 关键距离 {distance} Å · 异常 {anomaly}',
    '{formula} · {count} atoms · key distance {distance} Å · anomaly {anomaly}');
    return viewer;
  }

  function renderRepair(element, preview, onConfirm) {
    if (!preview || preview.ok === false) {
      element.textContent = (preview && preview.error) || tr('trajectory.repair.unavailable', {},
        '没有可用诊断证据；保持暂停。', 'No diagnosis evidence is available; remaining paused.');
      return;
    }
    const cost = preview.estimated_cost || {};
    const allowed = preview.execution_allowed === true;
    element.innerHTML =
      '<dl class="tp-repair-facts">' +
        `<dt>${VCS.esc(tr('trajectory.repair.signature', {}, '检测签名', 'Detected signature'))}</dt>` +
        `<dd>${VCS.esc(preview.failure_class || 'UNKNOWN')}</dd>` +
        `<dt>${VCS.esc(tr('trajectory.repair.evidence', {}, '检测证据', 'Evidence'))}</dt>` +
        `<dd>${VCS.esc(preview.detected_evidence || '')}</dd>` +
        `<dt>${VCS.esc(tr('trajectory.repair.suggestion', {}, '建议修改', 'Suggested change'))}</dt>` +
        `<dd>${VCS.esc(preview.suggested_change || '')}</dd>` +
        `<dt>${VCS.esc(tr('trajectory.repair.cost', {}, '预计成本', 'Estimated cost'))}</dt>` +
        `<dd>${VCS.esc(cost.class || 'unknown')} · ${VCS.esc(tr(
          'trajectory.repair.no_exact_time', {}, '不虚构精确机时', 'No fabricated exact machine time'))}</dd>` +
        `<dt>${VCS.esc(tr('trajectory.repair.impact', {}, '科学影响', 'Scientific impact'))}</dt>` +
        `<dd>${VCS.esc(preview.scientific_impact || '')}</dd>` +
      '</dl>' + methodDiffHtml(preview.method_diff || []) +
      `<p class="tp-pause-note">${VCS.esc(tr('trajectory.repair.default_pause', {},
        '默认决定：暂停。只有明确确认且计划仍绑定当前 source hash 时才允许调用既有有界续算。',
        'Default decision: pause. Execution is allowed only after explicit confirmation while the plan remains bound to the current source hash.'))}</p>` +
      (preview.manual_round_limit_override === true
        ? `<p class="warn-banner">${VCS.esc(tr('trajectory.repair.manual_override', {},
          '自动三轮上限已到；本计划只能由当前指纹绑定的明确人工确认授权第 4 轮。',
          'The automatic three-round cap has been reached. Only this fingerprint-bound explicit manual confirmation may authorize round 4.'))}</p>`
        : '') +
      (allowed ? `<button type="button" class="btn danger tp-confirm-repair">${VCS.esc(tr(
        'trajectory.repair.confirm', {}, '确认冻结 INCAR 续算', 'Confirm frozen-INCAR continuation'))}</button>` :
        `<p class="warn-banner">${VCS.esc(tr('trajectory.repair.manual_only', {},
          '该诊断不满足自动恢复边界；保持只读并交人工。',
          'This diagnosis is outside the automatic recovery boundary; it remains read-only for human review.'))}</p>`);
    const button = element.querySelector('.tp-confirm-repair');
    if (button) button.addEventListener('click', () => onConfirm(preview, button));
  }

  function createIntentGate() {
    let generation = 0;
    return {
      begin: () => { generation += 1; return generation; },
      valid: intent => intent === generation,
      invalidate: () => { generation += 1; },
    };
  }

  function trajectoryReceiptMatches(value, owner, frameToken) {
    if (!value || !owner) return false;
    if (String(value.session_token || '') !== owner.sessionToken) return false;
    if (String(value.source_hash || '') !== owner.sourceHash) return false;
    if (frameToken !== undefined &&
        String(value.frame_token || '') !== String(frameToken || '')) return false;
    return true;
  }

  function createFrameTokenRegistry(owner) {
    let tokens = new Set();
    let pageGeneration = 0;
    return {
      clear() { tokens = new Set(); pageGeneration += 1; },
      bind(page, generation) {
        tokens = new Set();
        pageGeneration = Number(generation || 0);
        if (!trajectoryReceiptMatches(page, owner)) return false;
        (page.rows || []).forEach(row => {
          const token = String(row && row.frame_token || '');
          if (token) tokens.add(token);
        });
        return true;
      },
      owns(token) { return tokens.has(String(token || '')); },
      generation() { return pageGeneration; },
    };
  }

  async function openPlayer(jobId, title, routeOwner) {
    const root = buildShell(title);
    let chart = null, viewer = null, closed = false;
    let overview = null, page = null, offset = 0, stride = 1;
    const intentGate = createIntentGate();
    let owner = null;
    let frameTokens = null;
    const modal = VCS.modal({
      title: tr('trajectory.title', { title }, '轨迹与诊断 — {title}', 'Trajectory and diagnostics — {title}'),
      body: root,
      actions: [{ label: tr('common.close', {}, '关闭', 'Close'), quiet: true,
        onClick: handle => handle.close() }],
    });
    modal.el.classList.add('modal-wide', 'trajectory-modal');
    const close = modal.close;
    modal.close = function () {
      if (closed) return;
      closed = true;
      intentGate.invalidate();
      if (frameTokens) frameTokens.clear();
      if (chart) chart.dispose();
      if (viewer) { try { viewer.clear(); } catch (_) { /* no-op */ } }
      document.removeEventListener('vcs:page', routeListener);
      if (window.Jobs && typeof window.Jobs.releaseTrajectoryOwner === 'function') {
        window.Jobs.releaseTrajectoryOwner(routeOwner);
      }
      close();
    };
    function routeListener(event) {
      const detail = event && event.detail;
      if (detail && !detail.overlay && detail.page !== 'jobs') modal.close();
    }
    document.addEventListener('vcs:page', routeListener);

    const status = root.querySelector('.tp-status');
    const body = root.querySelector('.tp-step-body');
    const pageNote = root.querySelector('.tp-page-note');
    const previous = root.querySelector('.tp-prev');
    const next = root.querySelector('.tp-next');
    const strideSelect = root.querySelector('.tp-stride');

    async function showFrame(token, inheritedIntent) {
      if (!token || closed || !frameTokens || !frameTokens.owns(token)) return;
      const intent = inheritedIntent === undefined
        ? intentGate.begin() : inheritedIntent;
      status.textContent = tr('trajectory.frame_loading', {}, '正在读取结构帧…', 'Loading structure frame…');
      const frame = await VCS.call('trajectory_frame', token);
      if (closed || !intentGate.valid(intent)) return;
      if (!frame || frame.ok === false) {
        status.textContent = (frame && frame.error) || tr('trajectory.frame_failed', {},
          '结构帧读取失败。', 'Failed to load the structure frame.');
        if (frame && frame.stale) status.className = 'tp-status warn-banner';
        return;
      }
      if (!trajectoryReceiptMatches(frame, owner, token)) {
        status.textContent = tr('trajectory.binding_invalid', {},
          '轨迹回执与当前播放器不匹配，已拒绝读取。',
          'The trajectory receipt does not belong to this player; the frame was rejected.');
        status.className = 'tp-status warn-banner';
        return;
      }
      if (viewer) { try { viewer.clear(); } catch (_) { /* no-op */ } viewer = null; }
      viewer = renderStructure(root.querySelector('.tp-canvas'),
        root.querySelector('.tp-frame-note'), frame);
      status.textContent = tr('trajectory.frame_ready', {}, '结构帧与服务端数值已同步。',
        'The structure frame and server values are synchronized.');
    }

    function bindRows() {
      body.querySelectorAll('.tp-step[data-frame-token]').forEach(button => {
        button.addEventListener('click', () => showFrame(button.dataset.frameToken));
      });
    }

    async function loadPage(wantedOffset) {
      const intent = intentGate.begin();
      frameTokens.clear();
      status.textContent = tr('trajectory.page_loading', {}, '正在读取分页步骤…', 'Loading paged steps…');
      const response = await VCS.call('trajectory_steps', overview.session_token,
        wantedOffset, 50, stride);
      if (closed || !intentGate.valid(intent)) return;
      if (!response || response.ok === false) {
        status.textContent = (response && response.error) || tr('trajectory.page_failed', {},
          '步骤页读取失败。', 'Failed to load the step page.');
        if (response && response.stale) status.className = 'tp-status warn-banner';
        return;
      }
      if (!frameTokens.bind(response, intent)) {
        status.textContent = tr('trajectory.binding_invalid', {},
          '轨迹回执与当前播放器不匹配，已拒绝读取。',
          'The trajectory receipt does not belong to this player; the response was rejected.');
        status.className = 'tp-status warn-banner';
        return;
      }
      page = response; offset = response.offset;
      body.innerHTML = rowsHtml(response); bindRows();
      previous.disabled = offset <= 0;
      next.disabled = response.next_offset === null;
      pageNote.textContent = tr('trajectory.page_summary', {
        start: response.rows.length ? offset + 1 : 0,
        end: offset + response.rows.length, total: response.sampled_steps,
        raw: response.total_steps, stride: response.stride,
      }, '显示抽样序列 {start}–{end} / {total}（原始 {raw} 步，间隔 {stride}）',
      'Showing sampled rows {start}–{end} / {total} ({raw} raw steps, stride {stride})');
      status.textContent = response.partial_write
        ? tr('trajectory.partial', {}, '源文件仍在写入；只显示完整步骤，刷新后可取得新快照。',
          'Sources are still being written; only complete steps are shown. Refresh for a new snapshot.')
        : tr('trajectory.ready', {}, '只读快照已就绪。', 'Read-only snapshot ready.');
      if (response.rows.length && response.rows[0].frame_token) {
        await showFrame(response.rows[0].frame_token, intent);
      }
    }

    previous.addEventListener('click', () => loadPage(Math.max(0, offset - 50)));
    next.addEventListener('click', () => {
      if (page && page.next_offset !== null) loadPage(page.next_offset);
    });
    strideSelect.addEventListener('change', () => {
      stride = Math.max(1, parseInt(strideSelect.value, 10) || 1);
      loadPage(0);
    });

    overview = await VCS.call('trajectory_open', String(jobId || ''));
    if (closed) return modal;
    if (!overview || overview.ok === false) {
      status.textContent = (overview && overview.error) || tr('trajectory.open_failed', {},
        '无法建立轨迹快照。', 'Could not create a trajectory snapshot.');
      status.className = 'tp-status warn-banner';
      return modal;
    }
    const requestedJobId = String(jobId || '');
    if (String(overview.job_id || '') !== requestedJobId ||
        !overview.session_token || !overview.source_hash ||
        !routeOwner || String(routeOwner.job_id || '') !== requestedJobId) {
      status.textContent = tr('trajectory.binding_invalid', {},
        '轨迹回执与当前播放器不匹配，已拒绝读取。',
        'The trajectory receipt does not belong to this player; the response was rejected.');
      status.className = 'tp-status warn-banner';
      return modal;
    }
    owner = Object.freeze({
      jobId: requestedJobId,
      sessionToken: String(overview.session_token),
      sourceHash: String(overview.source_hash),
    });
    frameTokens = createFrameTokenRegistry(owner);
    if (overview.task_kind !== 'aimd') {
      strideSelect.value = '1'; strideSelect.disabled = true;
    }
    root.querySelector('.tp-source').textContent = tr('trajectory.source_summary', {
      kind: overview.task_kind || 'unknown', steps: overview.n_steps || 0,
      frames: overview.n_frames || 0, hash: overview.source_hash || 'unknown',
      warnings: (overview.warnings || []).length
        ? ' · ' + (overview.warnings || []).join('；') : '',
    }, '类型 {kind} · {steps} 步 / {frames} 帧 · source SHA-256 {hash}{warnings}',
    'Type {kind} · {steps} steps / {frames} frames · source SHA-256 {hash}{warnings}');
    chart = renderChart(root.querySelector('.tp-chart'), overview);
    const repairIntent = intentGate.begin();
    const repair = await VCS.call('trajectory_repair_preview', overview.session_token);
    const repairOwned = !closed && intentGate.valid(repairIntent) && repair && repair.ok !== false &&
      trajectoryReceiptMatches(repair, owner) &&
      /^[0-9a-f]{64}$/i.test(String(repair.request_fingerprint || ''));
    if (!closed && (!repair || repair.ok === false || repairOwned)) {
      renderRepair(root.querySelector('.tp-repair-body'), repair,
      async (preview, button) => {
        if (!window.Jobs || typeof window.Jobs.confirmTrajectoryRepair !== 'function') return;
        button.disabled = true;
        const result = await window.Jobs.confirmTrajectoryRepair(
          overview.job_id, preview.plan_token, title,
          preview.request_fingerprint, routeOwner);
        if (closed) return;
        if (result && !result.error) {
          status.textContent = tr('trajectory.repair.queued', {},
            '恢复已提交；correction record 已写入，播放器快照现已 stale。',
            'Recovery submitted; a correction record was written and this snapshot is now stale.');
          status.className = 'tp-status warn-banner';
        } else {
          button.disabled = false;
        }
      });
    } else if (!closed) {
      renderRepair(root.querySelector('.tp-repair-body'), {
        ok: false,
        error: tr('trajectory.binding_invalid', {},
          '轨迹回执与当前播放器不匹配，已拒绝读取。',
          'The trajectory receipt does not belong to this player; the response was rejected.'),
      }, () => {});
    }
    await loadPage(0);
    return modal;
  }

  VCS.showTrajectory = openPlayer;
  if (window.__VCS_TEST__) {
    window.__VCS_TRAJECTORY_TEST__ = {
      rowsHtml, methodDiffHtml, buildShell, renderRepair, stepLabel, anomalyText,
      createIntentGate, trajectoryReceiptMatches, createFrameTokenRegistry,
    };
  }
})();
