// jobs.js — 作业页:台账表 + 全部批量动作 + 密码/信任流 + 自动刷新 + 集群队列/认领。
// 只依赖 app.js 暴露的 VCS.* 与 api 桥方法。行为逐条对齐 vcstudio/gui/jobs_tab.py。
// 全部数据插值走 VCS.esc(注入防御);零 emoji;中文文案。
'use strict';
(function () {
  const PROFILE_KEY = 'vcs.jobs.profile';
  const AUTO_KEY = 'vcs.jobs.auto';         // 自动刷新开关(持久化;默认开)
  const INT_KEY = 'vcs.jobs.interval';      // 自动刷新间隔(分钟,持久化)
  const $ = sel => document.querySelector(sel);
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

  const State = {
    rows: [],            // list_jobs 返回的行
    stale: [],           // 失效条目目录
    ledgerStatus: 'loading', // loading | ready | stale | unavailable
    ledgerError: '',
    lastLoadedAt: '',
    profiles: {},        // name -> profile dict
    selected: new Set(), // 选中的 dir
    expanded: new Set(), // 展开的项目组 key(默认全部折叠;本会话内记忆)
    selectionTrayExpanded: true,
    activeOperation: null,
    operationHistory: [],
    operationSequence: 0,
    refreshing: false,   // 查询在飞(auto 跳过本轮的护栏)
    autoTimer: null,
    reloadGeneration: 0,
    reloadInFlight: null,
    selectionGeneration: 0,
    resourceForecastStatus: 'idle',
    resourceForecastResult: null,
    resourceForecastGeneration: 0,
    resourceForecastSelection: '',
  };

  // 目录 → 末段名(本地日志用,兼容 \ 与 /)
  function base(p) { return String(p).replace(/[\\/]+$/, '').split(/[\\/]/).pop(); }
  function currentProfile() { const s = $('#jobs-profile'); return s ? s.value : ''; }
  function selectedDirs() { return Array.from(State.selected); }

  function stableJobId(row) {
    if (!row) return '';
    // 只接受后端真实作业标识，不用本机路径伪造可恢复 ID。
    const value = [row.job_uuid, row.id, row.job_id]
      .find(item => item !== null && item !== undefined && String(item).trim());
    return String(value === undefined ? '' : value).trim();
  }

  function publishJobContext(row) {
    const id = stableJobId(row);
    if (!id) return false;
    const projectId = String(row.project_id || '').trim();
    document.dispatchEvent(new CustomEvent('vcs:job-context', {
      detail: {
        id,
        job_id: id,
        job_uuid: String(row.job_uuid || ''),
        dir: String(row.dir || ''),
        name: String(row.name || ''),
        state: String(row.state || ''),
        project_id: projectId,
      },
    }));
    return true;
  }

  function publishJobContextCleared() {
    document.dispatchEvent(new CustomEvent('vcs:job-context', {
      detail: { id: null, job_id: null, clear: true },
    }));
  }

  function publishCurrentJobSelection(preferredDir) {
    const preferred = preferredDir && State.selected.has(preferredDir)
      ? State.rows.find(row => row.dir === preferredDir) : null;
    const row = preferred || State.rows.find(item => State.selected.has(item.dir)) || null;
    if (row) return publishJobContext(row);
    publishJobContextCleared();
    return false;
  }

  function currentWorkspaceProjectId() {
    const workspaceState = VCS.workspace && VCS.workspace.state;
    return String(workspaceState && workspaceState.project_id || '').trim();
  }

  function jobBelongsToWorkspaceProject(row) {
    const currentProjectId = currentWorkspaceProjectId();
    if (!currentProjectId) return true;
    const rowProjectId = String(row && row.project_id || '').trim();
    return !!rowProjectId && rowProjectId === currentProjectId;
  }

  function ensureTableScrollRegion() {
    const card = $('#jobs-card');
    if (!card) return null;
    card.classList.add('table-scroll', 'jobs-table-scroll-region');
    card.setAttribute('role', 'region');
    card.setAttribute('aria-label', tr('runtime.jobs.a11y.table_scroll', {},
      '作业列表，可水平滚动', 'Job list, horizontally scrollable'));
    card.setAttribute('tabindex', '0');
    return card;
  }

  function ensureFileScrollRegion() {
    const region = $('#fm-table');
    if (!region) return null;
    region.setAttribute('role', 'region');
    region.setAttribute('aria-label', tr('runtime.jobs.a11y.remote_files_scroll', {},
      '远程文件列表，可滚动', 'Remote file list, scrollable'));
    region.setAttribute('tabindex', '0');
    return region;
  }

  function syncTableSelection(card) {
    if (!card) return;
    card.querySelectorAll('tr[data-dir]').forEach(tr => {
      const selected = State.selected.has(tr.dataset.dir);
      tr.classList.toggle('sel', selected);
      tr.setAttribute('aria-selected', selected ? 'true' : 'false');
      const cb = tr.querySelector('.jrow-chk');
      if (cb) cb.checked = selected;
    });
  }

  // 所有破坏性/远程动作都必须与 job.yaml 绑定的服务器一致。后端还会做同样的
  // 硬校验；这里先给用户可理解的提示，避免输完密码后才发现选错服务器。
  function actionDirs(name, action, mode) {
    const rows = selectedDirs().map(d => State.rows.find(r => r.dir === d)).filter(Boolean);
    if (!rows.length) return [];
    let invalid;
    if (mode === 'new') {
      invalid = rows.filter(r => r.cluster || r.state !== 'CREATED');
      if (invalid.length) {
        VCS.log(tr('runtime.jobs.guard.created_only', {
          action, jobs: invalid.map(r => r.name).join(', '),
        }, '{action}仅适用于尚未提交的 CREATED 作业；已绑定/已提交的作业请走续算流程：{jobs}',
        '{action} only applies to unsubmitted CREATED jobs. Use the continuation workflow for bound or submitted jobs: {jobs}'), 'failc');
        return null;
      }
    } else {
      invalid = rows.filter(r => r.cluster !== name);
      if (invalid.length) {
        const jobs = invalid.map(r => `${r.name}(${r.cluster || tr(
          'runtime.jobs.state.not_submitted', {}, '尚未提交', 'Not submitted')})`).join(', ');
        VCS.log(tr('runtime.jobs.guard.wrong_server', { action, server: name, jobs },
          '{action}已阻止：所选作业不属于当前服务器「{server}」：{jobs}',
          '{action} blocked: the selected jobs do not belong to the current server "{server}": {jobs}'), 'failc');
        return null;
      }
    }
    return rows.map(r => r.dir);
  }

  function operationTargetRows(dirs) {
    const byDir = new Map(State.rows.map(row => [String(row.dir || ''), row]));
    return Array.from(new Set((dirs || []).map(String))).map(dir => byDir.get(dir)).filter(Boolean);
  }

  function operationTargetText(dirs) {
    return operationTargetRows(dirs).map((row, index) => {
      const name = String(row.name || base(row.dir));
      const project = String(row.project || tr('runtime.jobs.group.standalone', {},
        '单独作业', 'Standalone jobs'));
      const state = String(row.state || 'CREATED');
      const cluster = String(row.cluster || tr('runtime.jobs.selection.unbound', {},
        '未绑定集群', 'Unbound'));
      return tr('runtime.jobs.confirm.target_row', {
        index: index + 1, name, project, state, cluster,
      }, '{index}. {name}｜项目：{project}｜状态：{state}｜集群：{cluster}',
      '{index}. {name} | Project: {project} | Status: {state} | Cluster: {cluster}');
    }).join('\n');
  }

  function requireProfile() {
    const name = currentProfile();
    if (!name || !State.profiles[name]) {
      VCS.log(tr('runtime.jobs.profile.required', {},
        '请先在「集群」页配置并保存一个集群,再在上方选择目标集群',
        'Configure and save a cluster on the Cluster page, then select the target cluster above'), 'failc');
      return null;
    }
    return name;
  }

  // ── 台账取数 + 渲染 ────────────────────────────────────────────────────────
  function applyProfiles(r) {
    const profs = (r && r.profiles) || [];
    State.profiles = {};
    profs.forEach(p => { State.profiles[p.name] = p; });
    const sel = $('#jobs-profile');
    if (!sel) return;
    const saved = localStorage.getItem(PROFILE_KEY);
    const prev = sel.value;
    sel.innerHTML = '';
    if (!profs.length) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = tr('runtime.jobs.profile.none_configured', {},
        '(未配置集群)', '(No clusters configured)');
      sel.appendChild(o);
      return;
    }
    profs.forEach(p => {
      const o = document.createElement('option');
      o.value = p.name; o.textContent = p.name;
      sel.appendChild(o);
    });
    const want = [prev, saved].find(v => v && profs.some(p => p.name === v));
    sel.value = want || profs[0].name;
  }

  async function performReload(generation) {
    const [profiles, r] = await Promise.all([
      VCS.call('list_profiles'), VCS.call('list_jobs'),
    ]);
    // 后发刷新拥有列表；旧响应不得覆盖新工作区筛选、作业或选择。
    if (generation !== State.reloadGeneration) {
      return { ok: false, stale: true, generation };
    }
    applyProfiles(profiles);
    const validPayload = r && !r.error && Array.isArray(r.jobs) && Array.isArray(r.stale);
    if (validPayload) {
      invalidateResourceForecast();
      State.rows = r.jobs.map(row => {
        const clean = {};
        const rejected = new Set(['project' + '_path', 'project' + '_uuid']);
        Object.keys(row || {}).forEach(key => {
          if (!rejected.has(key)) clean[key] = row[key];
        });
        return clean;
      });
      State.stale = r.stale;
      State.ledgerStatus = 'ready';
      State.ledgerError = '';
      State.lastLoadedAt = new Date().toISOString();
    } else {
      // 读取失败时保留最后一次成功台账。把“服务不可用”误渲染成“没有作业”会诱导用户
      // 重复提交，且会让筛选外的选择上下文无声消失。
      State.ledgerStatus = State.lastLoadedAt ? 'stale' : 'unavailable';
      State.ledgerError = String((r && r.error) || tr(
        'runtime.jobs.ledger.invalid_response', {}, '台账返回格式无效',
        'The job ledger returned an invalid response'));
      VCS.log(tr('runtime.jobs.ledger.read_failed', { error: State.ledgerError },
        '读取台账失败:{error}', 'Failed to read job ledger: {error}'), 'failc');
      VCS.toast(tr('runtime.jobs.ledger.last_good_retained', {},
        '台账刷新失败；已保留上次成功读取的作业',
        'Ledger refresh failed; the last successfully loaded jobs are retained'), 'fail');
    }
    refreshClusterFilter();
    if (typeof fmClusters === 'function') fmClusters();
    renderTable();
    renderStats();
    renderStale();
    return { ok: true, stale: false, generation };
  }

  async function reload() {
    const generation = ++State.reloadGeneration;
    const pending = performReload(generation);
    State.reloadInFlight = { generation, promise: pending };
    let result = await pending;
    // 依赖 reload() 的提交流程应等到实际提交 DOM 的最新刷新，而非继续读旧 State。
    while (result && result.stale) {
      const latest = State.reloadInFlight;
      if (!latest || latest.generation <= result.generation) break;
      result = await latest.promise;
    }
    return result;
  }

  // 成员角色 → 中文标签(list_jobs 注入的 role 字段)
  function roleLabel(role) {
    const labels = {
      clean: tr('runtime.jobs.role.clean', {}, '清洁表面', 'Clean surface'),
      gas: tr('runtime.jobs.role.gas', {}, '气相参考', 'Gas-phase reference'),
      config: tr('runtime.jobs.role.config', {}, '吸附构型', 'Adsorption configuration'),
      molecule: tr('runtime.jobs.role.molecule', {}, '物种参考态', 'Species reference state'),
    };
    return labels[role] || '';
  }

  // 单行作业 HTML。grpKey 非空 → 属于某折叠组(data-grp);hidden → 组当前折叠
  function rowHtml(r, grpKey, hidden) {
    const task = r.task && r.task !== '/' ? r.task : '';
    const checked = State.selected.has(r.dir);
    const sel = checked ? ' class="sel"' : '';
    const role = roleLabel(r.role);
    const isQuick = String(r.task || '').startsWith('quick');
    const jobId = stableJobId(r);
    return `<tr data-dir="${VCS.esc(r.dir)}" data-name="${VCS.esc(r.name)}"` +
      (jobId ? ` data-job-id="${VCS.esc(jobId)}"` : '') +
      (grpKey ? ` data-grp="${VCS.esc(grpKey)}"` : '') +
      (hidden ? ' hidden' : '') + `${sel} tabindex="-1" aria-selected="${checked ? 'true' : 'false'}">` +
      `<td class="chk"><input type="checkbox" class="jrow-chk" aria-label="${VCS.esc(tr(
        'runtime.jobs.a11y.select_named_job', { name: r.name }, '选择作业 {name}', 'Select job {name}'))}"` +
      `${checked ? ' checked' : ''}></td>` +
      `<td>${VCS.elementBadge(r.name)}<span class="name">${VCS.esc(r.name)}</span>` +
      (role ? ` <span class="role-tag">${VCS.esc(role)}</span>` : '') +
      (r.cluster ? ` <span class="role-tag" title="${VCS.esc(tr(
        'runtime.jobs.table.bound_server_title', {}, '该作业绑定的服务器',
        'Server bound to this job'))}">${VCS.esc(r.cluster)}</span>` : '') +
      (task ? ` <span class="sub">${VCS.esc(task)}</span>` : '') +
      ` <button class="lnk conv" title="${VCS.esc(tr('runtime.jobs.action.convergence_title', {},
        '查看收敛过程(E0/ΔE/|F|max vs 离子步)',
        'View convergence (E0/ΔE/|F|max vs ionic step)'))}">${VCS.esc(tr(
        'runtime.jobs.action.convergence', {}, '收敛', 'Convergence'))}</button>` +
      (['RUNNING', 'QUEUED', 'SUBMITTED', 'UPLOADED'].indexOf(r.state) >= 0
        ? `<button class="lnk live" title="${VCS.esc(tr('runtime.jobs.action.live_title', {},
          '实时能量曲线:本地无 OSZICAR 时经 SSH 读远端,可轮询',
          'Live energy curve: poll the remote host over SSH when no local OSZICAR is available'))}">${VCS.esc(tr(
          'runtime.jobs.action.live', {}, '实时', 'Live'))}</button>` : '') +
      `<button class="lnk struct" title="${VCS.esc(tr('runtime.jobs.action.structure_title', {},
        '3D 结构预览(CONTCAR 优先,自动检查分子-衬底距离)',
        '3D structure preview (prefer CONTCAR and automatically check molecule-surface distance)'))}">${VCS.esc(tr(
        'runtime.jobs.action.structure', {}, '结构', 'Structure'))}</button>` +
      `<button class="lnk meth" title="${VCS.esc(tr('runtime.jobs.action.methods_title', {},
        '生成中英双语 Methods 段 + BibTeX(读真实 INCAR/KPOINTS/POTCAR)',
        'Generate bilingual Methods text and BibTeX from the actual INCAR/KPOINTS/POTCAR'))}">${VCS.esc(tr(
        'runtime.jobs.action.methods', {}, '方法', 'Methods'))}</button>` +
      `<button class="lnk dos" title="${VCS.esc(tr('runtime.jobs.action.dos_title', {},
        '总 DOS 出图(需本地 vasprun.xml)', 'Plot total DOS (requires a local vasprun.xml)'))}">DOS</button>` +
      (isQuick
        ? `<button class="lnk localrun" title="${VCS.esc(tr('runtime.jobs.action.local_run_title', {},
          '在本机跑该作业(需设置页配置本地软件命令)',
          'Run this job locally (configure the local software command on the Settings page)'))}">${VCS.esc(tr(
          'runtime.jobs.action.local_run', {}, '本机运行', 'Local run'))}</button>` : '') +
      (r.state === 'DONE'
        ? `<button class="lnk derive" title="${VCS.esc(tr('runtime.jobs.action.derive_title', {},
          '派生频率(ZPE)/电子结构静态/AIMD 作业',
          'Derive frequency (ZPE), electronic-structure static, or AIMD jobs'))}">${VCS.esc(tr(
          'runtime.jobs.action.derive', {}, '派生', 'Derive'))}</button>` : '') +
      `</td>` +
      `<td>${VCS.pill(r.state)}</td>` +
      `<td class="mono">${r.job_id ? VCS.esc(r.job_id) : '—'}</td>` +
      `<td class="num">${r.steps != null ? VCS.esc(r.steps) : '—'}</td>` +
      `<td class="num">${r.fmax != null ? VCS.esc(r.fmax) : '—'}</td>` +
      `<td class="num">${r.energy !== '' && r.energy != null ? VCS.esc(r.energy) : '—'}</td>` +
      `<td class="diag">${VCS.esc(r.diag || '')}</td>` +
      `<td class="mono">${VCS.esc(r.updated || '')}</td></tr>`;
  }

  // 组内状态计数(与 renderStats 同口径)
  function groupStats(rows) {
    const c = s => rows.filter(r => r.state === s).length;
    return {
      total: rows.length,
      done: c('DONE'),
      run: c('RUNNING'),
      queue: c('QUEUED') + c('SUBMITTED') + c('UPLOADED'),
      need: c('FAILED') + c('UNCONVERGED') + c('NEEDS_HUMAN'),
    };
  }

  function projectKindForGroup(group) {
    const allRows = State.rows.filter(row =>
      String(row.project_id || '') === String(group.projectId || ''));
    return allRows.length && allRows.every(row => row.role === 'molecule')
      ? 'molecule_library' : 'adsorption';
  }

  function groupBodyId(key) {
    let hash = 2166136261;
    const text = String(key || '');
    for (let i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return 'jobs-group-' + (hash >>> 0).toString(16);
  }

  // 组头行:项目名 + done/total 进度 + 细进度条 + 聚合状态;全 DONE 给「算 ΔE」
  function groupHeadHtml(key, label, rows, isProject, projectKind, projectId) {
    const st = groupStats(rows);
    const open = State.expanded.has(key);
    const pct = st.total ? Math.round(st.done / st.total * 100) : 0;
    let agg;
    if (st.need) agg = `<span class="pill fail"><i></i>${VCS.esc(tr(
      'runtime.jobs.group.needs_attention', { count: st.need }, '需处理 {count}',
      'Needs attention: {count}'))}</span>`;
    else if (st.run) agg = VCS.pill('RUNNING');
    else if (st.queue) agg = VCS.pill('QUEUED');
    else if (st.done === st.total) agg = VCS.pill('DONE');
    else agg = `<span class="pill q"><i></i>${VCS.esc(tr(
      'runtime.jobs.group.pending_submit', {}, '待提交', 'Pending submission'))}</span>`;
    const allDone = st.total > 0 && st.done === st.total;
    const action = open
      ? tr('runtime.jobs.group.collapse', {}, '折叠', 'Collapse')
      : tr('runtime.jobs.group.expand', {}, '展开', 'Expand');
    return `<tr class="grp-head" data-grp="${VCS.esc(key)}">` +
      '<td colspan="9">' +
      `<button type="button" class="btn quiet grp-toggle" data-group-toggle="${VCS.esc(key)}" ` +
      `aria-expanded="${open ? 'true' : 'false'}" aria-controls="${groupBodyId(key)}" ` +
      `aria-label="${VCS.esc(tr('runtime.jobs.group.toggle_aria', {
        action, group: label, count: st.total,
      }, '{action}{group}组，{count} 个作业', '{action} {group} group, {count} jobs'))}" ` +
      `title="${VCS.esc(tr('runtime.jobs.group.toggle_title', {
        action, count: st.total,
      }, '{action}组内 {count} 个作业', '{action} {count} jobs in this group'))}">` +
      `<span class="caret" aria-hidden="true">${open ? '▾' : '▸'}</span>` +
      `<b class="grp-name">${VCS.esc(label)}</b>` +
      (isProject ? `<span class="grp-tag">${projectKind === 'molecule_library'
        ? VCS.esc(tr('runtime.jobs.group.molecule_library', {}, '分子参考库', 'Molecule reference library'))
        : VCS.esc(tr('runtime.jobs.group.adsorption_project', {}, '吸附能项目', 'Adsorption-energy project'))}</span>` : '') +
      `<span class="grp-prog">${VCS.esc(tr('runtime.jobs.group.progress', {
        done: st.done, total: st.total,
      }, '{done}/{total} 完成', '{done}/{total} complete'))}</span>` +
      `<span class="grp-bar" aria-hidden="true"><i style="width:${pct}%"></i></span></button>` +
      agg +
      (isProject && projectKind !== 'molecule_library' && allDone
        ? ` <button class="btn grp-de" data-proj="${VCS.esc(label)}" ` +
          `data-project-id="${VCS.esc(projectId || '')}" ` +
          `title="${VCS.esc(tr('runtime.jobs.group.energy_title', {},
            '切到吸附能项目页并选中该项目',
            'Open the adsorption-energy project page and select this project'))}">${VCS.esc(tr(
            'runtime.jobs.group.energy_action', {}, '算 ΔE', 'Calculate ΔE'))}</button>`
        : '') +
      `</td></tr>`;
  }

  function renderTable() {
    const card = ensureTableScrollRegion();
    if (!card) return;
    const hadSelection = State.selected.size > 0;
    if (!State.rows.length) {
      State.selected.clear();
      if (hadSelection) publishJobContextCleared();
      const unavailable = State.ledgerStatus === 'unavailable';
      card.innerHTML = `<div class="empty"><p>${VCS.esc(unavailable
        ? tr('runtime.jobs.empty.ledger_unavailable', {},
          '作业台账暂时不可用；这不表示当前没有作业，请稍后重试',
          'The job ledger is temporarily unavailable. This does not mean there are no jobs; retry shortly')
        : tr('runtime.jobs.empty.none_managed', {},
          '还没有纳管的作业 — 去生成页产出四件套,或从集群队列认领已有作业',
          'No managed jobs yet. Create a four-file input set on the Generate page or claim an existing job from the cluster queue'))}</p></div>`;
      renderLedgerState();
      renderSelectionTray();
      return;
    }
    // 丢弃已不在台账里的选中项
    const present = new Set(State.rows.map(r => r.dir));
    State.selected.forEach(d => { if (!present.has(d)) State.selected.delete(d); });
    if (hadSelection && !State.selected.size) publishJobContextCleared();

    const rows0 = visibleRows();
    if (!rows0.length) {
      card.innerHTML = `<div class="empty"><p>${VCS.esc(tr('runtime.jobs.empty.no_filter_match', {},
        '当前筛选无匹配作业 — 调整上方筛选条件',
        'No jobs match the current filters. Adjust the filters above'))}</p></div>`;
      renderLedgerState();
      renderSelectionTray();
      return;
    }

    // 按吸附能项目分桶(project=null → 单独作业桶);保持筛选后次序
    const groups = new Map();
    const single = [];
    rows0.forEach(r => {
      const rowProjectId = String(r.project_id || '').trim();
      if (r.project && rowProjectId) {
        const groupKey = 'id:' + rowProjectId;
        if (!groups.has(groupKey)) {
          groups.set(groupKey, {
            label: r.project,
            projectId: rowProjectId,
            rows: [],
          });
        }
        groups.get(groupKey).rows.push(r);
      } else {
        single.push(r);
      }
    });

    let h = `<table aria-label="${VCS.esc(tr('runtime.jobs.table.label', {},
      '作业列表', 'Job list'))}"><thead><tr>` +
      `<th scope="col" class="chk" aria-label="${VCS.esc(tr('runtime.jobs.table.select_column', {},
        '选择作业', 'Select jobs'))}"></th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.table.job', {}, '作业', 'Job'))}</th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.table.status', {}, '状态', 'Status'))}</th>` +
      `<th scope="col" class="mono">${VCS.esc(tr('runtime.jobs.table.job_id', {}, '作业号', 'Job ID'))}</th>` +
      `<th scope="col" class="num">${VCS.esc(tr('runtime.jobs.table.steps', {}, '步', 'Steps'))}</th>` +
      '<th scope="col" class="num">|F|max</th>' +
      `<th scope="col" class="num">E0 (eV)</th><th scope="col">${VCS.esc(tr(
        'runtime.jobs.table.diagnosis', {}, '诊断', 'Diagnosis'))}</th>` +
      `<th scope="col" class="mono">${VCS.esc(tr('runtime.jobs.table.updated', {},
        '更新', 'Updated'))}</th></tr></thead>`;
    if (!groups.size) {
      // 没有任何项目组 → 保持旧平铺观感,不加组头
      h += '<tbody>';
      rows0.forEach(r => { h += rowHtml(r, null, false); });
      h += '</tbody>';
    } else {
      groups.forEach((group, groupKey) => {
        const rows = group.rows;
        const key = 'p:' + groupKey;
        const open = State.expanded.has(key);
        const projectKind = projectKindForGroup(group);
        h += '<tbody class="job-group-heading">' + groupHeadHtml(
          key, group.label, rows, true, projectKind, group.projectId) + '</tbody>' +
          `<tbody id="${groupBodyId(key)}" class="job-group-body">`;
        rows.forEach(r => { h += rowHtml(r, key, !open); });
        h += '</tbody>';
      });
      if (single.length) {
        const key = 's:_single';
        const open = State.expanded.has(key);
        h += '<tbody class="job-group-heading">' +
          groupHeadHtml(key, tr('runtime.jobs.group.standalone', {}, '单独作业', 'Standalone jobs'),
            single, false, '', '') + '</tbody>' +
          `<tbody id="${groupBodyId(key)}" class="job-group-body">`;
        single.forEach(r => { h += rowHtml(r, key, !open); });
        h += '</tbody>';
      }
    }
    h += '</table>';
    card.innerHTML = h;
    renderLedgerState();
    renderSelectionTray();
  }

  function renderLedgerState() {
    const el = $('#jobs-ledger-state');
    if (!el) return;
    if (State.ledgerStatus === 'ready') {
      el.hidden = true;
      el.textContent = '';
      el.className = 'jobs-ledger-state';
      return;
    }
    el.hidden = false;
    const hasLastGood = State.ledgerStatus === 'stale';
    el.className = 'jobs-ledger-state ' + (hasLastGood ? 'stale' : 'unavailable');
    el.textContent = hasLastGood
      ? tr('runtime.jobs.ledger.showing_last_good', { error: State.ledgerError },
        '当前显示上次成功读取的数据；本次刷新失败：{error}',
        'Showing the last successfully loaded data; this refresh failed: {error}')
      : tr('runtime.jobs.ledger.unavailable', { error: State.ledgerError },
        '作业台账不可用：{error}', 'Job ledger unavailable: {error}');
  }

  function selectedRows() {
    return State.rows.filter(row => State.selected.has(row.dir));
  }

  function resourceForecastSelectionKey(rows) {
    const ids = (rows || []).map(stableJobId).filter(Boolean).sort();
    return ids.length === (rows || []).length ? ids.join('\n') : '';
  }

  function invalidateResourceForecast(selectionKey) {
    State.resourceForecastGeneration += 1;
    State.resourceForecastStatus = 'idle';
    State.resourceForecastResult = null;
    State.resourceForecastSelection = selectionKey === undefined
      ? resourceForecastSelectionKey(selectedRows()) : selectionKey;
  }

  function resourceRiskLabel(value) {
    const labels = {
      no_matching_history: ['jobs.forecast.risk.no_history', '无匹配历史', 'No matching history'],
      sparse_history: ['jobs.forecast.risk.sparse_history', '历史样本稀少', 'Sparse history'],
      high_recent_failure_rate: ['jobs.forecast.risk.failure_rate', '近期失败率高', 'High recent failure rate'],
      wide_uncertainty: ['jobs.forecast.risk.wide', '不确定区间较宽', 'Wide uncertainty'],
      budget_at_risk: ['jobs.forecast.risk.budget', '剩余预算有风险', 'Remaining budget at risk'],
    };
    const label = labels[value];
    return label ? tr(label[0], {}, label[1], label[2]) : String(value);
  }

  function renderResourceForecast() {
    const panel = $('#jobs-resource-forecast');
    const output = $('#jobs-resource-forecast-out');
    const button = $('#jb-tray-forecast');
    const rows = selectedRows();
    if (!panel || !output || !button) return;
    const key = resourceForecastSelectionKey(rows);
    if (key !== State.resourceForecastSelection) invalidateResourceForecast(key);
    button.disabled = !key || State.resourceForecastStatus === 'loading';
    panel.hidden = State.resourceForecastStatus === 'idle';
    panel.className = `jobs-resource-forecast ${State.resourceForecastStatus}`;
    panel.setAttribute('aria-busy', State.resourceForecastStatus === 'loading' ? 'true' : 'false');
    if (State.resourceForecastStatus === 'idle') {
      output.replaceChildren();
      return;
    }
    if (State.resourceForecastStatus === 'loading') {
      output.textContent = tr('jobs.forecast.loading', {},
        '正在从服务端台账与清单重建证据…',
        'Reconstructing evidence from the server ledger and manifests…');
      return;
    }
    const result = State.resourceForecastResult || {};
    if (!result.ok || State.resourceForecastStatus === 'unavailable' || !result.display) {
      output.textContent = result.error || tr('jobs.forecast.unavailable', {},
        '证据不足，资源估算不可用；不会猜测缺失值',
        'Resource forecast unavailable because evidence is incomplete; missing values are not guessed');
      return;
    }
    const display = result.display;
    const denom = result.denominators || {};
    const risks = ((result.forecast || {}).risk_flags || []).map(resourceRiskLabel);
    const historyReasons = Object.entries(result.history_missing_reasons || {}).map(
      ([reason, count]) => `${reason}: ${count}`);
    const unknown = (result.unknown_jobs || []).map(item =>
      `${item.job_id}: ${(item.missing || []).join(', ')}`);
    const statusLabel = State.resourceForecastStatus === 'at_risk'
      ? tr('jobs.forecast.at_risk', {}, '预算风险', 'At risk')
      : tr('jobs.forecast.ready', {}, '估算就绪', 'Ready');
    const summary = [
      `<div class="jobs-resource-forecast-summary"><b>${VCS.esc(statusLabel)}</b>` +
        `<span>${VCS.esc(tr('jobs.forecast.point', { value: display.estimate_core_hours },
          '点估计 {value} 核时', 'Point estimate: {value} core-hours'))}</span>` +
        `<span>${VCS.esc(tr('jobs.forecast.range', { value: display.range_core_hours },
          '区间 {value} 核时', 'Range: {value} core-hours'))}</span>` +
        `<span>${VCS.esc(tr('jobs.forecast.confidence', { value: display.confidence },
          '置信度 {value}', 'Confidence: {value}'))}</span></div>`,
      `<div class="jobs-resource-forecast-meta">${VCS.esc(tr(
        'jobs.forecast.denominator', {
          ready: denom.forecastable_jobs, total: denom.selected_jobs,
          samples: denom.usable_history_entries,
        }, '可估算 {ready}/{total}；可用历史 {samples} 条',
        '{ready}/{total} forecastable; {samples} usable history records'))}<br>` +
        `${VCS.esc(tr('jobs.forecast.budget', {
          value: display.remaining_budget_core_hours,
          status: display.budget_status, evidence: result.budget_evidence_status,
        }, '剩余预算：{value} 核时；状态 {status}；证据 {evidence}',
        'Remaining budget: {value} core-hours; status: {status}; evidence: {evidence}'))}<br>` +
        `${VCS.esc(tr('jobs.forecast.history_unknown', {
          count: denom.unknown_history_entries,
          reasons: historyReasons.length ? historyReasons.join(', ') : '—',
        }, '未采用历史 {count} 条；缺项 {reasons}',
        '{count} history records not used; missing evidence: {reasons}'))}<br>` +
        `${VCS.esc(tr('jobs.forecast.history_hash', {
          hash: result.history_basis_sha256,
        }, '历史依据哈希：{hash}', 'History-basis hash: {hash}'))}</div>`,
    ];
    const jobRows = (display.jobs || []).map(item =>
      `<li><b>${VCS.esc(item.job_id)}</b><span>${VCS.esc(tr(
        'jobs.forecast.job_row', {
          point: item.estimate_core_hours, range: item.range_core_hours,
          success: item.matching_successful_samples,
          observed: item.matching_observed_samples,
          confidence: item.confidence, failure: item.failure_rate,
        }, '点估计 {point}；区间 {range} 核时；成功样本 {success}/{observed}；置信度 {confidence}；失败率 {failure}',
        'Point {point}; range {range} core-hours; successful samples {success}/{observed}; confidence {confidence}; failure rate {failure}'))}</span></li>`).join('');
    if (jobRows) summary.push(`<ul>${jobRows}</ul>`);
    if (risks.length) summary.push(`<p class="warn">${VCS.esc(tr(
      'jobs.forecast.risks', { risks: risks.join('；') },
      '风险：{risks}', 'Risks: {risks}'))}</p>`);
    if (unknown.length) summary.push(`<p class="unknown">${VCS.esc(tr(
      'jobs.forecast.unknown_jobs', { jobs: unknown.join('；') },
      '缺证据作业：{jobs}', 'Jobs with missing evidence: {jobs}'))}</p>`);
    summary.push(`<p class="boundary">${VCS.esc(tr('jobs.forecast.boundary', {},
      '此估算仅供建议；不授权提交。提交仍须经过原确认与幂等门。',
      'This forecast is advisory only and does not authorize submission. The existing confirmation and idempotency gates still apply.'))}</p>`);
    output.innerHTML = summary.join('');
  }

  async function estimateSelectedResources() {
    const rows = selectedRows();
    const key = resourceForecastSelectionKey(rows);
    const ids = rows.map(stableJobId);
    if (!key || ids.length !== rows.length) {
      State.resourceForecastStatus = 'unavailable';
      State.resourceForecastResult = { ok: false, error: tr(
        'jobs.forecast.missing_ids', {},
        '所选作业缺少稳定服务端标识，无法估算',
        'A selected job lacks a stable server ID, so it cannot be forecast') };
      renderResourceForecast();
      return false;
    }
    const generation = ++State.resourceForecastGeneration;
    State.resourceForecastSelection = key;
    State.resourceForecastStatus = 'loading';
    State.resourceForecastResult = null;
    renderResourceForecast();
    try {
      const result = await VCS.call('jobs_resource_forecast', ids);
      if (generation !== State.resourceForecastGeneration ||
          key !== resourceForecastSelectionKey(selectedRows())) return false;
      State.resourceForecastResult = result || { ok: false };
      State.resourceForecastStatus = result && result.ok
        ? (result.status || 'unavailable') : 'unavailable';
      renderResourceForecast();
      return State.resourceForecastStatus === 'ready' ||
        State.resourceForecastStatus === 'at_risk';
    } catch (error) {
      if (generation !== State.resourceForecastGeneration) return false;
      State.resourceForecastResult = { ok: false, error: String(error && error.message || error) };
      State.resourceForecastStatus = 'unavailable';
      renderResourceForecast();
      return false;
    }
  }

  function renderSelectionTray() {
    const tray = $('#jobs-selection-tray');
    const list = $('#jobs-selection-list');
    const summary = $('#jobs-selection-summary');
    const toggle = $('#jobs-selection-review');
    if (!tray || !list || !summary || !toggle) return;
    const rows = selectedRows();
    const selectionKey = resourceForecastSelectionKey(rows);
    if (selectionKey !== State.resourceForecastSelection) invalidateResourceForecast(selectionKey);
    tray.hidden = rows.length === 0;
    if (!rows.length) {
      list.innerHTML = '';
      summary.textContent = '';
      renderResourceForecast();
      return;
    }
    const visible = new Set(visibleRows().map(row => row.dir));
    const hiddenCount = rows.filter(row => !visible.has(row.dir)).length;
    summary.textContent = hiddenCount
      ? tr('runtime.jobs.selection.summary_hidden', {
        count: rows.length, hidden: hiddenCount,
      }, '已选 {count} 个，其中 {hidden} 个被筛选条件隐藏',
      '{count} selected; {hidden} hidden by the current filters')
      : tr('runtime.jobs.selection.summary', { count: rows.length },
        '已选 {count} 个作业', '{count} jobs selected');
    toggle.setAttribute('aria-expanded', State.selectionTrayExpanded ? 'true' : 'false');
    toggle.textContent = State.selectionTrayExpanded
      ? tr('runtime.jobs.selection.collapse', {}, '收起明细', 'Collapse details')
      : tr('runtime.jobs.selection.expand', {}, '查看明细', 'Review details');
    list.hidden = !State.selectionTrayExpanded;
    list.innerHTML = rows.map(row => {
      const project = String(row.project || tr('runtime.jobs.group.standalone', {},
        '单独作业', 'Standalone jobs'));
      const cluster = String(row.cluster || tr('runtime.jobs.selection.unbound', {},
        '未绑定集群', 'Unbound'));
      return `<li><div><b>${VCS.esc(row.name || base(row.dir))}</b>` +
        `<span>${VCS.esc(project)} · ${VCS.esc(cluster)}</span></div>` +
        `${VCS.pill(row.state || 'CREATED')}</li>`;
    }).join('');
    renderResourceForecast();
  }

  const OPERATION_BUTTONS = [
    'jb-submit', 'jb-fetch', 'jb-continue', 'jb-cancelchecked', 'jb-remove',
    'jb-tray-submit', 'jb-tray-fetch', 'jb-tray-continue', 'jb-tray-cancel',
    'jb-tray-remove',
  ];

  function operationId(kind) {
    State.operationSequence += 1;
    return 'jobop-' + Date.now().toString(36) + '-' + State.operationSequence.toString(36) +
      '-' + String(kind || 'operation').replace(/[^A-Za-z0-9_.-]/g, '-');
  }

  function setOperationControlsBusy(busy) {
    OPERATION_BUTTONS.forEach(id => {
      const button = $('#' + id);
      if (button) button.disabled = !!busy;
    });
  }

  function renderOperationQueue() {
    const box = $('#jobs-operation-queue');
    const active = $('#jobs-operation-active');
    const history = $('#jobs-operation-history');
    if (!box || !active || !history) return;
    const op = State.activeOperation;
    box.hidden = !op && !State.operationHistory.length;
    active.hidden = !op;
    active.innerHTML = op
      ? `<b>${VCS.esc(op.label)}</b><span>${VCS.esc(tr(
        'runtime.jobs.operation.active', { count: op.dirs.length, status: op.status },
        '{count} 个作业 · {status}', '{count} jobs · {status}'))}</span>`
      : '';
    history.innerHTML = State.operationHistory.slice(0, 5).map(item =>
      `<li class="${VCS.esc(item.status)}"><b>${VCS.esc(item.label)}</b>` +
      `<span>${VCS.esc(tr('runtime.jobs.operation.history_item', {
        count: item.dirs.length, status: item.status,
      }, '{count} 个作业 · {status}', '{count} jobs · {status}'))}</span></li>`).join('');
    setOperationControlsBusy(!!op);
  }

  function publishWorkspaceOperation(op) {
    if (!op || !VCS.operations || typeof VCS.operations.publish !== 'function') return;
    VCS.operations.publish({
      id: op.id, kind: op.kind, label: op.label, status: op.status,
      count: op.dirs.length, error: op.error || '', route: 'run-jobs',
      started_at: op.startedAt, updated_at: op.finishedAt || new Date().toISOString(),
    });
  }

  function updateOperation(op, status) {
    if (!op || State.activeOperation !== op) return false;
    op.status = status;
    renderOperationQueue();
    publishWorkspaceOperation(op);
    return true;
  }

  async function withExclusiveOperation(kind, label, dirs, profile, work) {
    if (State.activeOperation) {
      VCS.toast(tr('runtime.jobs.operation.busy', { operation: State.activeOperation.label },
        '另一项作业操作“{operation}”仍在进行，请等待其完成',
        'Another job operation, "{operation}", is still active; wait for it to finish'), 'fail');
      return null;
    }
    const op = {
      id: operationId(kind), kind, label, profile: String(profile || ''),
      dirs: Array.from(new Set((dirs || []).map(String))).sort(),
      status: 'confirming', startedAt: new Date().toISOString(),
    };
    State.activeOperation = op;
    renderOperationQueue();
    publishWorkspaceOperation(op);
    let outcome = { status: 'failed' };
    try {
      outcome = (await work(op)) || { status: 'cancelled' };
    } catch (error) {
      outcome = { status: 'failed', error: String(error && error.message || error) };
      VCS.log(tr('runtime.jobs.operation.failed', { operation: label, error: outcome.error },
        '{operation}失败：{error}', '{operation} failed: {error}'), 'failc');
    } finally {
      if (State.activeOperation === op) {
        op.status = outcome.status || 'failed';
        op.finishedAt = new Date().toISOString();
        op.error = String(outcome.error || '');
        State.activeOperation = null;
        State.operationHistory.unshift(op);
        State.operationHistory = State.operationHistory.slice(0, 20);
        renderOperationQueue();
        publishWorkspaceOperation(op);
      }
    }
    return outcome.value === undefined ? outcome : outcome.value;
  }

  // ── 筛选(集群 / 状态)+ 排序(时间↓/名称/状态) ──
  const STATUS_GROUP = {
    run: ['RUNNING'], queue: ['QUEUED', 'SUBMITTED', 'UPLOADED'], done: ['DONE'],
    fail: ['FAILED', 'UNCONVERGED'], need: ['NEEDS_HUMAN'],
  };
  function visibleRows() {
    const fc = ($('#jf-cluster') && $('#jf-cluster').value) || '';
    const fs = ($('#jf-status') && $('#jf-status').value) || '';
    const so = ($('#jf-sort') && $('#jf-sort').value) || 'time';
    let rows = State.rows.slice();
    if (fc) rows = rows.filter(r => (r.cluster || '') === fc);
    if (fs && STATUS_GROUP[fs]) rows = rows.filter(r => STATUS_GROUP[fs].indexOf(r.state) >= 0);
    if (so === 'name') rows.sort((a, b) => String(a.name).localeCompare(String(b.name)));
    else if (so === 'state') rows.sort((a, b) => String(a.state).localeCompare(String(b.state)));
    else rows.sort((a, b) => String(b.updated || '').localeCompare(String(a.updated || '')));
    return rows;
  }

  function workspaceJobControlValue(queryKey, stateKey) {
    const workspace = VCS.workspace;
    if (!workspace) return '';
    const route = workspace.current;
    if (route && route.def && route.def.page === 'jobs' && route.query && route.query[queryKey]) {
      return String(route.query[queryKey]);
    }
    const workspaceState = workspace.state || {};
    const filters = workspaceState.filters || {};
    const sort = workspaceState.sort || {};
    if (stateKey === 'jobs') return String(sort.jobs || '');
    return String(filters[stateKey] || '');
  }

  function explicitJobRouteFilter(key) {
    const workspace = VCS.workspace;
    const route = workspace && workspace.current;
    if (!route || !route.def || route.def.page !== 'jobs' || !route.query) return '';
    return String(route.query[key] || '');
  }

  function jobMatchesStatusFilter(row, filter) {
    return !filter || !STATUS_GROUP[filter] || STATUS_GROUP[filter].indexOf(row.state) >= 0;
  }

  function revealSelectedJob(row) {
    const explicitCluster = explicitJobRouteFilter('cluster');
    const explicitStatus = explicitJobRouteFilter('status');
    if ((explicitCluster && String(row.cluster || '') !== explicitCluster) ||
        !jobMatchesStatusFilter(row, explicitStatus)) return false;

    const cluster = $('#jf-cluster');
    if (cluster && cluster.value && String(row.cluster || '') !== cluster.value) {
      cluster.value = '';
      cluster.dispatchEvent(new Event('change', { bubbles: true }));
    }
    const status = $('#jf-status');
    if (status && status.value && !jobMatchesStatusFilter(row, status.value)) {
      status.value = '';
      status.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return true;
  }

  function refreshClusterFilter() {
    const sel = $('#jf-cluster');
    if (!sel) return;
    const cur = sel.value;
    // workspace.js 可能在 list_jobs 返回前尝试恢复筛选；此时动态集群选项尚不存在。
    // 生成选项后重新读取当前工作区状态，避免首次恢复退回“全部集群”。
    const restored = workspaceJobControlValue('cluster', 'job_cluster');
    const wanted = restored || cur;
    const clusters = Array.from(new Set([
      ...Object.keys(State.profiles), ...State.rows.map(r => r.cluster).filter(Boolean),
    ]));
    // 深链/恢复值即使暂时没有作业也必须保持为显式零结果筛选，不能悄悄回退到全部。
    if (wanted && clusters.indexOf(wanted) < 0) clusters.push(wanted);
    sel.innerHTML = `<option value="">${VCS.esc(tr('runtime.jobs.filter.all_clusters', {},
      '全部集群', 'All clusters'))}</option>` +
      clusters.map(c => `<option value="${VCS.esc(c)}">${VCS.esc(c)}</option>`).join('');
    if (!wanted || clusters.indexOf(wanted) >= 0) {
      sel.value = wanted;
      // 深链查询可能早于动态 option 到达；只有此处确认可恢复后才回写 workspace state。
      if (restored && cur !== wanted) sel.dispatchEvent(new Event('change', { bubbles: true }));
    }

    const status = $('#jf-status');
    const restoredStatus = workspaceJobControlValue('status', 'job_status');
    if (status && restoredStatus && Array.from(status.options || []).some(
      option => option.value === restoredStatus)) status.value = restoredStatus;
    const sort = $('#jf-sort');
    const restoredSort = workspaceJobControlValue('', 'jobs');
    if (sort && restoredSort && Array.from(sort.options || []).some(
      option => option.value === restoredSort)) sort.value = restoredSort;
  }

  // 组头折叠开关:记忆到 State.expanded(重载/自动刷新后保持)
  function toggleGroup(key, restoreFocus) {
    if (State.expanded.has(key)) State.expanded.delete(key);
    else State.expanded.add(key);
    renderTable();
    if (restoreFocus) {
      const button = Array.from(document.querySelectorAll('[data-group-toggle]'))
        .find(item => item.dataset.groupToggle === key);
      if (button) {
        try { button.focus({ preventScroll: true }); } catch (_) { button.focus(); }
      }
    }
  }

  function projectRecordId(project) {
    return String(project && (project.project_id || project.id) || '').trim();
  }

  function knownProject(projectId) {
    const wantedId = String(projectId || '').trim();
    if (!wantedId) return null;
    const workspaceProjects = VCS.workspace && Array.isArray(VCS.workspace.projects)
      ? VCS.workspace.projects : [];
    const projectPageProjects = window.Project && typeof window.Project.list === 'function'
      ? window.Project.list() : [];
    return workspaceProjects.concat(projectPageProjects || [])
      .find(project => projectRecordId(project) === wantedId) || null;
  }

  // 「算 ΔE」:只按 opaque project_id 原子同步 workspace + Project 业务页。
  async function gotoProject(projectId) {
    const workspace = VCS.workspace;
    const targetId = String(projectId || '').trim();
    let target = knownProject(targetId);
    if (workspace && typeof workspace.refresh === 'function' &&
        !target) {
      await workspace.refresh();
      target = knownProject(targetId);
    }
    if (!targetId || !target || !window.Project ||
        typeof window.Project.selectById !== 'function') {
      VCS.log(tr('runtime.jobs.project.open_unverifiable', {},
        '无法打开项目：任务缺少可验证的 project_id',
        'Cannot open project: the job lacks a verifiable project_id'), 'failc');
      VCS.toast(tr('runtime.jobs.project.unconfirmed', {},
        '无法确认任务所属项目', 'Cannot determine which project owns this job'), 'fail');
      return false;
    }

    if (workspace && typeof workspace.requestProjectSwitch === 'function' &&
        typeof workspace.navigateRoute === 'function') {
      const switched = await workspace.requestProjectSwitch(targetId,
        () => window.Project.selectById(targetId));
      if (!switched) return false;
      const out = await workspace.navigateRoute('project-overview', {
        projectId: targetId,
        source: 'jobs-project',
      });
      return !!(out && out.ok);
    }

    const selected = await window.Project.selectById(targetId);
    if (!selected) return false;
    const out = await VCS.navigate('project', { source: 'jobs-project' });
    return !!(out && out.ok);
  }

  // 「导出报告」:报告在项目页生成 —— 跳项目页 + toast + 选中选中作业所属项目
  async function doReport() {
    const dirs = selectedDirs();
    let projName = '';
    let projectId = '';
    if (dirs.length) {
      const row = State.rows.find(r => r.dir === dirs[0]);
      if (row && row.project) {
        projName = row.project;
        projectId = String(row.project_id || '').trim();
      }
    }
    const opened = await gotoProject(projectId);
    if (!opened) return;
    VCS.toast(tr('runtime.jobs.report.generated_on_project_page', {},
      '报告在项目页生成', 'Generate the report on the project page'));
    VCS.log(projName
      ? tr('runtime.jobs.report.project_selected', { project: projName },
        '报告在「吸附能项目」页生成(已为你选中项目「{project}」)',
        'Generate the report on the Adsorption Energy Project page (project "{project}" is selected)')
      : tr('runtime.jobs.report.project_page', {},
        '报告在「吸附能项目」页生成',
        'Generate the report on the Adsorption Energy Project page'));
  }

  function renderStats() {
    const el = $('#jobs-stats');
    if (!el) return;
    const n = State.rows.length;
    const c = s => State.rows.filter(r => r.state === s).length;
    const run = c('RUNNING');
    const queue = c('QUEUED') + c('SUBMITTED') + c('UPLOADED');
    const done = c('DONE');
    const need = c('FAILED') + c('UNCONVERGED') + c('NEEDS_HUMAN');
    el.innerHTML = tr('runtime.jobs.stats.summary', {
      total: n, run: `<b>${run}</b>`, queue: `<b>${queue}</b>`,
      done: `<b>${done}</b>`, need: `<b>${need}</b>`,
    }, '{total} 作业 · {run} RUN · {queue} QUEUE · {done} DONE · {need} 需处理',
    '{total} jobs · {run} RUN · {queue} QUEUE · {done} DONE · {need} need attention');
  }

  function renderStale() {
    const el = $('#jobs-stale');
    if (!el) return;
    el.textContent = State.stale.length
      ? tr('runtime.jobs.stale.hidden_summary', { count: State.stale.length },
        '已隐藏 {count} 个失效条目(目录或 job.yaml 已不存在)——点「清理失效条目」一键移出台账',
        '{count} stale entries are hidden because their directory or job.yaml no longer exists. Select "Clean stale entries" to remove them from the ledger')
      : '';
  }

  // ── 选中:单击单选、ctrl/cmd 多选;双击打开目录 ───────────────────────────
  function bindTable() {
    const card = $('#jobs-card');
    if (!card) return;
    card.addEventListener('click', e => {
      // 组头行只由真实按钮控制；表格行本身不冒充按钮。
      const gh = e.target.closest('tr.grp-head');
      if (gh) {
        const de = e.target.closest('.grp-de');
        if (de) {
          e.stopPropagation();
          gotoProject(de.dataset.projectId || '');
          return;
        }
        const toggle = e.target.closest('[data-group-toggle]');
        if (toggle) toggleGroup(toggle.dataset.groupToggle, true);
        return;
      }
      const tr = e.target.closest('tr[data-dir]');
      if (!tr) return;
      // 「实时」按钮:打开实时能量曲线面板(v3.3.0,不参与行选中)
      if (e.target.closest('.live')) {
        e.stopPropagation();
        const row = State.rows.find(x => x.dir === tr.dataset.dir) || {};
        liveOpen(tr.dataset.dir, tr.dataset.name || tr.dataset.dir, row.cluster || '');
        return;
      }
      // 「收敛」/「结构」按钮:打开对应视图,不参与行选中
      if (e.target.closest('.conv')) {
        e.stopPropagation();
        VCS.showConvergence(tr.dataset.dir, tr.dataset.name || tr.dataset.dir);
        return;
      }
      if (e.target.closest('.struct')) {
        e.stopPropagation();
        VCS.showStructure(tr.dataset.dir, 'AUTO', tr.dataset.name || tr.dataset.dir);
        return;
      }
      if (e.target.closest('.meth')) {
        e.stopPropagation();
        VCS.showMethods(tr.dataset.dir, tr.dataset.name || tr.dataset.dir);
        return;
      }
      if (e.target.closest('.dos')) {
        e.stopPropagation();
        VCS.showDos(tr.dataset.dir, tr.dataset.name || tr.dataset.dir);
        return;
      }
      if (e.target.closest('.derive')) {
        e.stopPropagation();
        toggleDeriveRow(tr);
        return;
      }
      if (e.target.closest('.localrun')) {
        e.stopPropagation();
        toggleLocalRow(tr);
        return;
      }
      const dir = tr.dataset.dir;
      // 勾选框:切换选中(不清空其他选中)
      if (e.target.closest('.jrow-chk')) {
        State.selectionGeneration += 1;
        if (State.selected.has(dir)) State.selected.delete(dir);
        else State.selected.add(dir);
        syncTableSelection(card);
        publishCurrentJobSelection(dir);
        return;
      }
      State.selectionGeneration += 1;
      if (e.ctrlKey || e.metaKey) {
        if (State.selected.has(dir)) State.selected.delete(dir);
        else State.selected.add(dir);
      } else {
        State.selected.clear();
        State.selected.add(dir);
      }
      syncTableSelection(card);
      publishCurrentJobSelection(dir);
    });
    card.addEventListener('dblclick', async e => {
      const tr = e.target.closest('tr[data-dir]');
      if (!tr) return;
      const r = await VCS.call('open_dir', tr.dataset.dir);
      if (r && r.error) VCS.log(tr('runtime.jobs.directory.open_failed', { error: r.error },
        '打开目录失败:{error}', 'Failed to open directory: {error}'), 'failc');
    });
  }

  // ── 远程动作通用壳:密码获取 → NEED_PASSWORD 重试 → needs_trust 确认重试 ──
  // invoke(password, trust) 必须返回 api dict。返回最终 dict,或 null(用户取消)。
  async function acquirePassword(name) {
    const prof = State.profiles[name];
    if (!prof || prof.auth !== 'password') return { ok: true, password: null };
    const hp = await VCS.call('has_saved_password', name);
    if (hp && hp.saved) return { ok: true, password: null };  // keyring 里有,后端自取
    const pw = await VCS.password();
    if (pw === null) return { ok: false };
    return { ok: true, password: pw };
  }

  async function remote(name, invoke) {
    const acq = await acquirePassword(name);
    if (!acq.ok) {
      VCS.log(tr('runtime.jobs.remote.cancelled_no_password', {},
        '已取消(未输入密码)', 'Cancelled (no password entered)'));
      return null;
    }
    let password = acq.password;
    let trust = false;
    let res = await invoke(password, trust);
    for (;;) {
      if (!res) return res;
      if (res.cancelled) return null;
      if (res.needPassword) { password = res.password; res = await invoke(password, trust); continue; }
      if (res.needs_trust) {
        const pin = await VCS.confirmHostKey(res);
        if (!pin) {
          VCS.log(tr('runtime.jobs.remote.unknown_host_blocked', {},
            '已取消或阻止未知主机连接', 'Unknown-host connection cancelled or blocked'), 'failc');
          return null;
        }
        trust = pin;
        res = await invoke(password, trust);
        continue;
      }
      return res;
    }
  }

  // 结果逐条日志。triple: [dir, ok, msg];否则 [dir, msg]
  function logResults(results, triple) {
    (results || []).forEach(row => {
      if (triple) {
        const [dir, ok, msg] = row;
        VCS.log(tr('runtime.jobs.common.job_message', {
          name: base(dir), message: msg,
        }, '{name}:{message}', '{name}: {message}'), ok ? 'okc' : 'failc');
      } else {
        const [dir, msg] = row;
        VCS.log(tr('runtime.jobs.common.job_message', {
          name: base(dir), message: msg,
        }, '{name}:{message}', '{name}: {message}'));
      }
    });
  }

  // ── 派生计算展开区(DONE 作业):频率(ZPE)/ 电子结构静态多选 ────────────────
  function fmtChange(c) {
    if (typeof c === 'string') return tr('runtime.jobs.common.raw_message', { message: c },
      '{message}', '{message}');                     // estatic 改动为字符串
    const key = c.key, act = c.action;
    if (act === 'add') return tr('runtime.jobs.change.added', {
      key, value: c.new, reason: c.reason ? ` (${c.reason})` : '',
    }, '新增 {key} = {value}{reason}', 'Added {key} = {value}{reason}');
    if (act === 'strip') return tr('runtime.jobs.change.removed', {
      key, old: c.old, reason: c.reason ? `: ${c.reason}` : '',
    }, '剥离 {key}(原 {old}){reason}', 'Removed {key} (previously {old}){reason}');
    return tr('runtime.jobs.change.updated', {
      key, old: c.old, value: c.new, reason: c.reason ? ` (${c.reason})` : '',
    }, '{key}:{old} → {value}{reason}', '{key}: {old} → {value}{reason}');
  }

  function toggleDeriveRow(tr) {
    const dir = tr.dataset.dir, name = tr.dataset.name || dir;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('derive-row') && next.dataset.for === dir) {
      next.remove(); return;                         // 再点收起
    }
    const row = document.createElement('tr');
    row.className = 'derive-row';
    row.dataset.for = dir;
    row.innerHTML = '<td colspan="9"><div class="derive-box">' +
      `<span class="derive-lbl">${VCS.esc(tr('runtime.jobs.derive.heading', {},
        '派生计算:', 'Derived calculations:'))}</span>` +
      `<button class="btn quiet" data-dfreq>${VCS.esc(tr('runtime.jobs.derive.frequency', {},
        '频率 (ZPE)', 'Frequency (ZPE)'))}</button>` +
      `<span class="derive-sep">${VCS.esc(tr('runtime.jobs.derive.electronic_static', {},
        '电子结构静态:', 'Electronic-structure static:'))}</span>` +
      '<label><input type="checkbox" data-k="pdos" checked> PDOS</label>' +
      '<label><input type="checkbox" data-k="bader"> Bader</label>' +
      `<label><input type="checkbox" data-k="chgdiff"> ${VCS.esc(tr(
        'runtime.jobs.derive.charge_difference', {}, '差分电荷', 'Charge difference'))}</label>` +
      `<button class="btn quiet" data-dstatic>${VCS.esc(tr('runtime.jobs.derive.static_action', {},
        '派生静态', 'Derive static jobs'))}</button>` +
      `<span class="derive-sep">${VCS.esc(tr('runtime.jobs.derive.aimd_stability', {},
        'AIMD 热稳定性:', 'AIMD thermal stability:'))}</span>` +
      `<label>${VCS.esc(tr('runtime.jobs.derive.ensemble', {}, '系综', 'Ensemble'))} ` +
      '<select class="ipt" data-aimd-ens style="width:auto;display:inline-block">' +
      '<option value="nvt">NVT</option><option value="nve">NVE</option></select></label>' +
      `<label>${VCS.esc(tr('runtime.jobs.derive.temperature', {}, '温度', 'Temperature'))} ` +
      '<input class="ipt" data-aimd-temp value="300" style="width:56px"> K</label>' +
      `<label>${VCS.esc(tr('runtime.jobs.derive.steps', {}, '步数', 'Steps'))} ` +
      '<input class="ipt" data-aimd-steps value="10000" style="width:72px"></label>' +
      `<button class="btn quiet" data-daimd>${VCS.esc(tr('runtime.jobs.derive.aimd_action', {},
        '派生 AIMD', 'Derive AIMD'))}</button></div></td>`;
    tr.parentNode.insertBefore(row, tr.nextSibling);
    row.querySelector('[data-dfreq]').addEventListener('click', () => doDeriveFreq(dir, name));
    row.querySelector('[data-dstatic]').addEventListener('click', () => {
      const kinds = Array.from(row.querySelectorAll('input[data-k]:checked')).map(c => c.dataset.k);
      doDeriveEstatic(dir, name, kinds);
    });
    row.querySelector('[data-daimd]').addEventListener('click', () => {
      const ens = row.querySelector('[data-aimd-ens]').value;
      const temp = parseFloat(row.querySelector('[data-aimd-temp]').value) || 300;
      const steps = parseInt(row.querySelector('[data-aimd-steps]').value, 10) || 10000;
      doDeriveAimd(dir, name, ens, temp, steps);
    });
  }

  async function doDeriveAimd(dir, name, ens, temp, steps) {
    VCS.log(tr('runtime.jobs.derive.aimd_start', {
      ensemble: ens.toUpperCase(), temperature: temp, steps, name,
    }, '派生 AIMD 作业({ensemble} {temperature}K {steps}步):{name} …',
    'Deriving AIMD job ({ensemble} {temperature} K, {steps} steps): {name} …'));
    const r = await VCS.call('derive_aimd', dir, ens, temp, null, steps, 1.0, null);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.jobs.derive.aimd_failed', {
        error: (r && r.error) || tr('runtime.jobs.common.unknown_error', {},
          '未知错误', 'Unknown error'),
      }, '派生 AIMD 失败:{error}', 'Failed to derive AIMD job: {error}'), 'failc'); return;
    }
    VCS.log(tr('runtime.jobs.derive.aimd_created', { path: r.job_dir },
      '已派生 AIMD 作业:{path}', 'Derived AIMD job: {path}'), 'okc');
    (r.changes || []).forEach(c => VCS.log('  · ' + fmtChange(c)));
    (r.warnings || []).forEach(w => VCS.log(tr('runtime.jobs.common.indented_warning', {
      message: w,
    }, '  ⚠ {message}', '  WARNING: {message}'), 'warnc'));
    VCS.log(tr('runtime.jobs.derive.aimd_registered', {},
      'AIMD 作业已入台账,去列表提交',
      'The AIMD job is in the ledger; submit it from the job list'), 'okc');
    await reload();
  }

  // ── 本机运行展开区(quick/gaussian 作业):启动 / 轮询状态 / 停止 ──
  const LocalRun = { timers: {} };
  function toggleLocalRow(tr) {
    const dir = tr.dataset.dir, name = tr.dataset.name || dir;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('localrun-row') && next.dataset.for === dir) {
      stopLocalPoll(dir); next.remove(); return;
    }
    const row = document.createElement('tr');
    row.className = 'localrun-row';
    row.dataset.for = dir;
    row.innerHTML = '<td colspan="9"><div class="derive-box">' +
      `<span class="derive-lbl">${VCS.esc(tr('runtime.jobs.local.heading', {},
        '本机运行:', 'Local run:'))}</span>` +
      `<label>${VCS.esc(tr('runtime.jobs.local.command_template', {},
        '命令模板', 'Command template'))} <input class="ipt" data-lr-cmd ` +
      `placeholder="${VCS.esc(tr('runtime.jobs.local.command_placeholder', {},
        '如 g16 或 g16 {input} {output}', 'For example, g16 or g16 {input} {output}'))}" ` +
      'style="width:280px"></label>' +
      `<button class="btn quiet" data-lr-start>${VCS.esc(tr('runtime.jobs.local.start', {},
        '启动', 'Start'))}</button>` +
      `<button class="btn quiet" data-lr-stop>${VCS.esc(tr('runtime.jobs.local.stop', {},
        '停止', 'Stop'))}</button>` +
      `<span class="lr-state sub" data-lr-state>${VCS.esc(tr('runtime.jobs.local.not_running', {},
        '未运行', 'Not running'))}</span>` +
      '<pre class="mono lr-log" data-lr-log style="margin:6px 0 0;max-height:16vh"></pre></div></td>';
    tr.parentNode.insertBefore(row, tr.nextSibling);
    row.querySelector('[data-lr-start]').addEventListener('click', () => localStart(dir, name, row));
    row.querySelector('[data-lr-stop]').addEventListener('click', () => localStop(dir, row));
    localPoll(dir, row);   // 立即拉一次状态
  }
  async function localStart(dir, name, row) {
    const tmpl = (row.querySelector('[data-lr-cmd]').value || '').trim();
    if (!tmpl) {
      VCS.toast(tr('runtime.jobs.local.command_required', {},
        '请填入本机命令模板(如 g16)', 'Enter a local command template (for example, g16)'), 'fail');
      return;
    }
    VCS.log(tr('runtime.jobs.local.starting', { name, command: tmpl },
      '本机运行启动:{name}({command})…', 'Starting local run: {name} ({command})…'));
    const r = await VCS.call('local_run_start', dir, tmpl);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.jobs.local.start_failed', {
        error: (r && r.error) || tr('runtime.jobs.common.unknown_error', {},
          '未知错误', 'Unknown error'),
      }, '本机运行启动失败:{error}', 'Failed to start local run: {error}'), 'failc'); return;
    }
    VCS.log(tr('runtime.jobs.local.started', { pid: r.pid, command: (r.cmd || []).join(' ') },
      '本机运行已启动:pid {pid}({command})', 'Local run started: PID {pid} ({command})'), 'okc');
    startLocalPoll(dir, row);
  }
  async function localStop(dir, row) {
    const r = await VCS.call('local_run_cancel', dir);
    if (r && r.ok) {
      VCS.log(tr('runtime.jobs.local.stopped', { name: base(dir) },
        '已停止本机作业:{name}', 'Stopped local job: {name}'), 'okc');
      localPoll(dir, row);
    } else VCS.log(tr('runtime.jobs.local.stop_failed', {
      error: (r && r.error) || tr('runtime.jobs.common.unknown', {}, '未知', 'Unknown'),
    }, '停止失败:{error}', 'Failed to stop local run: {error}'), 'failc');
  }
  async function localPoll(dir, row) {
    const r = await VCS.call('local_run_status', dir);
    if (!r || !r.ok) return;
    const st = row.querySelector('[data-lr-state]');
    if (st) st.textContent = r.exit_code != null
      ? tr('runtime.jobs.local.status_with_exit', { state: r.state || '?', code: r.exit_code },
        '状态:{state}(退出码 {code})', 'Status: {state} (exit code {code})')
      : tr('runtime.jobs.local.status', { state: r.state || '?' },
        '状态:{state}', 'Status: {state}');
    const log = row.querySelector('[data-lr-log]');
    if (log) log.textContent = tr('runtime.jobs.common.raw_message', {
      message: r.log_tail || '',
    }, '{message}', '{message}');
    if (r.state === 'DONE' || r.state === 'FAILED') stopLocalPoll(dir);
  }
  function startLocalPoll(dir, row) {
    stopLocalPoll(dir);
    LocalRun.timers[dir] = setInterval(() => localPoll(dir, row), 4000);
    localPoll(dir, row);
  }
  function stopLocalPoll(dir) {
    if (LocalRun.timers[dir]) { clearInterval(LocalRun.timers[dir]); delete LocalRun.timers[dir]; }
  }

  async function doDeriveFreq(dir, name) {
    VCS.log(tr('runtime.jobs.derive.frequency_start', { name },
      '派生频率作业(ZPE):{name} …', 'Deriving frequency job (ZPE): {name} …'));
    const r = await VCS.call('derive_freq', dir);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.jobs.derive.frequency_failed', {
        error: (r && r.error) || tr('runtime.jobs.common.unknown_error', {},
          '未知错误', 'Unknown error'),
      }, '派生频率失败:{error}', 'Failed to derive frequency job: {error}'), 'failc'); return;
    }
    VCS.log(tr('runtime.jobs.derive.frequency_created', { path: r.job_dir },
      '已派生频率作业:{path}', 'Derived frequency job: {path}'), 'okc');
    (r.changes || []).forEach(c => VCS.log('  · ' + fmtChange(c)));
    (r.warnings || []).forEach(w => VCS.log(tr('runtime.jobs.common.indented_warning', {
      message: w,
    }, '  ⚠ {message}', '  WARNING: {message}'), 'warnc'));
    VCS.log(tr('runtime.jobs.derive.frequency_registered', {},
      '频率作业已入台账,去列表提交',
      'The frequency job is in the ledger; submit it from the job list'), 'okc');
    await reload();
  }

  async function doDeriveEstatic(dir, name, kinds) {
    if (!kinds.length) {
      VCS.toast(tr('runtime.jobs.derive.static_kind_required', {},
        '请至少勾选一种静态类型', 'Select at least one static calculation type'), 'fail');
      return;
    }
    VCS.log(tr('runtime.jobs.derive.static_start', { kinds: kinds.join(', '), name },
      '派生静态作业({kinds}):{name} …', 'Deriving static jobs ({kinds}): {name} …'));
    const r = await VCS.call('derive_estatic', dir, kinds);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.jobs.derive.static_failed', {
        error: (r && r.error) || tr('runtime.jobs.common.unknown_error', {},
          '未知错误', 'Unknown error'),
      }, '派生静态失败:{error}', 'Failed to derive static jobs: {error}'), 'failc'); return;
    }
    (r.jobs || []).forEach(j => {
      VCS.log(tr('runtime.jobs.derive.static_created', { kind: j.kind, path: j.job_dir },
        '已派生 {kind} 静态作业:{path}', 'Derived {kind} static job: {path}'), 'okc');
      (j.changes || []).forEach(c => VCS.log('  · ' + fmtChange(c)));
      (j.warnings || []).forEach(w => VCS.log(tr('runtime.jobs.common.indented_warning', {
        message: w,
      }, '  ⚠ {message}', '  WARNING: {message}'), 'warnc'));
    });
    (r.skipped || []).forEach(s => VCS.log(tr('runtime.jobs.derive.skipped', {
      kind: s.kind, reason: s.reason,
    }, '跳过 {kind}:{reason}', 'Skipped {kind}: {reason}'), 'warnc'));
    VCS.log(tr('runtime.jobs.derive.static_registered', {},
      '静态作业已入台账,去列表提交',
      'The static jobs are in the ledger; submit them from the job list'), 'okc');
    await reload();
  }

  // ── 自旋对比:对选中的自旋家族判基态 + 磁矩审计 ────────────────────────────
  async function doSpinCompare() {
    const dirs = selectedDirs();
    if (dirs.length < 2) {
      VCS.log(tr('runtime.jobs.spin.selection_required', {},
        '自旋对比:请选中同一家族的多个自旋变体(_spin_nm/_ls/_hs)',
        'Spin comparison: select multiple spin variants from the same family (_spin_nm/_ls/_hs)'), 'failc'); return;
    }
    VCS.log(tr('runtime.jobs.spin.start', { count: dirs.length },
      '自旋对比({count} 个变体)…', 'Comparing {count} spin variants…'));
    const r = await VCS.call('spin_family_compare', dirs);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.jobs.spin.failed', {
        error: (r && r.error) || tr('runtime.jobs.common.unknown_error', {},
          '未知错误', 'Unknown error'),
      }, '自旋对比失败:{error}', 'Spin comparison failed: {error}'), 'failc'); return;
    }
    const g = r.ground || {};
    if (g.pending && g.pending.length) {
      VCS.log(tr('runtime.jobs.spin.pending', { jobs: g.pending.join(', ') },
        '尚未全部 DONE,待完成:{jobs}', 'Not all jobs are DONE; still pending: {jobs}'), 'warnc');
    } else if (g.winner) {
      VCS.log(tr('runtime.jobs.spin.ground_state', {
        winner: g.winner,
        energies: Object.entries(g.de_meV || {}).map(kv => kv[0] + '=' + kv[1] + ' meV').join(', '),
      }, '自旋基态:{winner}(相对能量 {energies})',
      'Spin ground state: {winner} (relative energies: {energies})'), 'okc');
      if (g.warning) VCS.log(tr('runtime.jobs.common.warning', { message: g.warning },
        '⚠ {message}', 'WARNING: {message}'), 'warnc');
    }
    (r.audits || []).forEach(a => {
      if (!a.audited) VCS.log(tr('runtime.jobs.spin.audit_unavailable', {
        name: a.name,
        message: a.warning || tr('runtime.jobs.spin.cannot_audit', {}, '无法审计', 'Cannot audit'),
      }, '磁矩审计 {name}:{message}', 'Magnetic-moment audit {name}: {message}'), 'warnc');
      else if (a.warning) VCS.log(tr('runtime.jobs.spin.audit_warning', {
        name: a.name, warning: a.warning,
      }, '磁矩审计 {name}:⚠ {warning}', 'Magnetic-moment audit {name}: WARNING {warning}'), 'warnc');
      else VCS.log(tr('runtime.jobs.spin.audit_ok', {
        name: a.name, magnetization: a.final_magnetization,
      }, '磁矩审计 {name}:末态 {magnetization} μB,正常',
      'Magnetic-moment audit {name}: final {magnetization} μB, normal'), 'okc');
    });
    if (r.report_file) VCS.log(tr('runtime.jobs.spin.report_created', { path: r.report_file },
      '多自旋态可追溯报告:{path}', 'Traceable multi-spin report: {path}'), 'okc');
    if (r.report_error) VCS.log(tr('runtime.jobs.spin.report_failed', { error: r.report_error },
      '多自旋报告生成失败:{error}', 'Failed to generate multi-spin report: {error}'), 'warnc');
  }

  // ── 上传并提交 ─────────────────────────────────────────────────────────────
  async function doSubmit() {
    const name = requireProfile();
    if (!name) return;
    const actionLabel = tr('runtime.jobs.submit.action_name', {}, '提交', 'Submit');
    const dirs = actionDirs(name, actionLabel, 'new');
    if (dirs === null) return;
    if (!dirs.length) {
      VCS.log(tr('runtime.jobs.submit.selection_required', {},
        '请先在列表中选中要提交的作业(可多选)',
        'Select one or more jobs to submit from the list'), 'failc');
      return;
    }
    return withExclusiveOperation('submit', actionLabel, dirs, name, async op => {
      const prof = State.profiles[name];
      const ok = await VCS.confirm(tr('runtime.jobs.submit.confirm', {
        count: dirs.length,
        server: name,
        remote_root: prof.remote_root || tr('runtime.jobs.common.not_set', {}, '(未设置)', '(Not set)'),
        script_mode: prof.script_mode,
      }, '将上传并提交 {count} 个作业到「{server}」\n远程根目录:{remote_root}\n脚本模式:{script_mode}\n\n继续?',
      'Upload and submit {count} jobs to "{server}"?\nRemote root: {remote_root}\nScript mode: {script_mode}\n\nContinue?'));
      if (!ok) return { status: 'cancelled' };
      updateOperation(op, 'running');
      VCS.log(tr('runtime.jobs.submit.connecting', { count: dirs.length },
        '连接并提交 {count} 个作业…', 'Connecting and submitting {count} jobs…'));
      const res = await remote(name, (pw, trust) =>
        VCS.call('submit_jobs', dirs, name, pw, trust, op.id));
      if (!res) return { status: 'cancelled' };
      if (res.error) {
        VCS.log(tr('runtime.jobs.submit.failed', { error: res.error },
          '提交异常:{error}', 'Submission failed: {error}'), 'failc');
        return { status: 'failed', error: res.error, value: res };
      }
      logResults(res.results, true);
      await reload();
      return { status: 'succeeded', value: res };
    });
  }

  // ── 查询状态(手动 / 自动共用) ────────────────────────────────────────────
  async function runStatus(auto) {
    const name = auto ? currentProfile() : requireProfile();
    if (!name || !State.profiles[name]) return;   // auto 无集群静默跳过
    if (State.refreshing) {
      if (!auto) VCS.log(tr('runtime.jobs.status.query_in_progress', {},
        '上一轮查询仍在进行,请稍候', 'The previous status query is still running; please wait'));
      return;
    }
    if (auto) {
      const prof = State.profiles[name];
      if (prof.auth === 'password') {
        const hp = await VCS.call('has_saved_password', name);
        if (!(hp && hp.saved)) {
          VCS.log(tr('runtime.jobs.status.auto_needs_saved_password', {},
            '自动刷新需先手动查询一次并保存密码,本轮跳过',
            'Automatic refresh requires one manual query with a saved password; skipping this cycle'), 'failc');
          return;
        }
      }
    }
    State.refreshing = true;
    try {
      if (!auto) VCS.log(tr('runtime.jobs.status.querying', {},
        '查询作业状态…', 'Querying job status…'));
      const res = await remote(name, (pw, trust) => VCS.call('refresh_status', name, pw, trust));
      if (!res) return;
      if (res.error) {
        VCS.log(tr('runtime.jobs.status.query_failed', {
          mode: auto
            ? tr('runtime.jobs.status.auto_refresh', {}, '自动刷新', 'Automatic refresh')
            : tr('runtime.jobs.status.manual_query', {}, '查询', 'Status query'),
          error: res.error,
        }, '{mode}异常:{error}', '{mode} failed: {error}'), 'failc');
        // 自动刷新失败在别页也要可见(此前只写日志,用户看不到)
        if (auto) VCS.toast(tr('runtime.jobs.status.auto_failed', { error: res.error },
          '自动刷新失败:{error}', 'Automatic refresh failed: {error}'), 'fail');
        return;
      }
      const results = res.results || [];
      if (!results.length) {
        if (!auto) VCS.log(tr('runtime.jobs.status.none_active_for_server', { server: name },
          '「{server}」没有待查询的作业(SUBMITTED/QUEUED/RUNNING)',
          'No jobs on "{server}" need a status query (SUBMITTED/QUEUED/RUNNING)'));
      } else {
        logResults(results, false);
      }
      await reload();
    } finally {
      State.refreshing = false;
    }
  }

  async function runAllStatus(auto) {
    if (State.refreshing) {
      if (!auto) VCS.log(tr('runtime.jobs.status.query_in_progress', {},
        '上一轮查询仍在进行,请稍候', 'The previous status query is still running; please wait'));
      return;
    }
    State.refreshing = true;
    try {
      if (!auto) VCS.log(tr('runtime.jobs.status.querying_all', {},
        '并行查询所有服务器上的活跃作业…', 'Querying active jobs on all servers in parallel…'));
      const res = await VCS.call('refresh_all_status');
      if (!res || res.error) {
        VCS.log(tr('runtime.jobs.status.all_failed', {
          error: (res && res.error) || tr('runtime.jobs.common.unknown_error', {},
            '未知错误', 'Unknown error'),
        }, '全部服务器查询失败:{error}', 'All-server status query failed: {error}'), 'failc');
        return;
      }
      let count = 0;
      (res.profiles || []).forEach(server => {
        if (server.error) {
          const next = server.error === 'NEED_PASSWORD'
            ? tr('runtime.jobs.status.server_needs_password', {},
              '请在目标集群下手动查询一次并保存密码',
              'Run one manual query on the target cluster and save the password')
            : server.needs_trust ? tr('runtime.jobs.status.server_needs_trust', {},
              '请先选择该集群手动查询并核对 SHA256 指纹',
              'Select this cluster, run a manual query, and verify the SHA256 fingerprint') : server.error;
          VCS.log(tr('runtime.jobs.status.server_failed', { server: server.name, error: next },
            '[{server}] 查询失败:{error}', '[{server}] Query failed: {error}'), 'failc');
          return;
        }
        (server.results || []).forEach(row => {
          count++;
          VCS.log(tr('runtime.jobs.status.server_job_message', {
            server: server.name, job: row[0], message: row[1],
          }, '[{server}] {job}: {message}', '[{server}] {job}: {message}'));
        });
      });
      if (!count && !auto && !(res.errors || []).length) {
        VCS.log(tr('runtime.jobs.status.none_active_anywhere', {},
          '所有服务器都没有待查询的活跃作业', 'No server has active jobs that need a status query'));
      }
      if ((res.errors || []).length && auto) {
        VCS.toast(tr('runtime.jobs.status.multi_server_attention', { count: res.errors.length },
          '多服务器监控：{count} 台需要处理，其它服务器已正常刷新',
          'Multi-server monitor: {count} servers need attention; all other servers refreshed normally'), 'fail');
      }
      await reload();
    } finally {
      State.refreshing = false;
    }
  }

  // ── 拉回结果:默认按每个 job.yaml 的任务类型选择关键产物 ─────────────────
  function askFetchFiles(n) {
    return new Promise(resolve => {
      const presets = [
        [tr('runtime.jobs.fetch.preset_auto', {},
          '按任务自动（推荐）：能带 / DOS / Bader / ELF / 功函数 / AIMD 各取自己的关键文件',
          'Automatic by task (recommended): retrieve the key files for bands, DOS, Bader, ELF, work function, or AIMD'), null],
        [tr('runtime.jobs.fetch.preset_light', {},
          '仅轻量：CONTCAR + OSZICAR + OUTCAR',
          'Lightweight only: CONTCAR + OSZICAR + OUTCAR'), ['CONTCAR', 'OSZICAR', 'OUTCAR']],
        [tr('runtime.jobs.fetch.preset_electronic_full', {},
          '电子结构全家桶：轻量 + vasprun.xml + DOSCAR + EIGENVAL + CHGCAR + AECCAR0/2 + ELFCAR + LOCPOT',
          'Full electronic-structure set: lightweight files + vasprun.xml + DOSCAR + EIGENVAL + CHGCAR + AECCAR0/2 + ELFCAR + LOCPOT'),
          ['CONTCAR', 'OSZICAR', 'OUTCAR', 'vasprun.xml', 'DOSCAR', 'EIGENVAL',
            'CHGCAR', 'AECCAR0', 'AECCAR2', 'ACF.dat', 'ELFCAR', 'LOCPOT', 'XDATCAR']],
      ];
      let done = false;
      const finish = v => { if (!done) { done = true; resolve(v); } };
      const body =
        presets.map((p, i) => `<label style="display:block;margin-bottom:8px"><input type="radio" name="fp" value="${i}"${i === 0 ? ' checked' : ''}> ${VCS.esc(p[0])}</label>`).join('') +
        `<label style="display:block;margin-bottom:6px"><input type="radio" name="fp" value="99"> ${VCS.esc(tr(
          'runtime.jobs.fetch.custom_files', {}, '自定义(逗号分隔文件名):',
          'Custom (comma-separated filenames):'))}</label>` +
        `<input id="fp-custom" class="ipt" value="CONTCAR, OUTCAR, vasprun.xml">`;
      const m = VCS.modal({
        title: tr('runtime.jobs.fetch.modal_title', { count: n },
          '拉回结果 — {count} 个作业', 'Retrieve results — {count} jobs'),
        bodyHTML: body,
        actions: [
          { label: tr('runtime.jobs.common.cancel', {}, '取消', 'Cancel'),
            quiet: true, onClick: mm => { mm.close(); finish(null); } },
          { label: tr('runtime.jobs.fetch.start', {}, '开始拉回', 'Start retrieval'),
            primary: true, onClick: mm => {
              const c = mm.el.querySelector('input[name=fp]:checked').value;
              let files;
              if (c === '99') {
                files = mm.el.querySelector('#fp-custom').value
                  .split(',').map(s => s.trim()).filter(Boolean);
                if (!files.length) {
                  VCS.toast(tr('runtime.jobs.fetch.custom_empty', {},
                    '自定义文件列表为空', 'The custom file list is empty'), 'fail');
                  return;
                }
              } else {
                files = presets[+c][1];
              }
              mm.close(); finish({
                files,
                label: c === '99' ? tr('runtime.jobs.fetch.custom_package', {},
                  '自定义结果包', 'Custom result package') : presets[+c][0],
              });
            } },
        ],
      });
      m.onDismiss = () => finish(null);
    });
  }

  async function doFetch() {
    const name = requireProfile();
    if (!name) return;
    const actionLabel = tr('runtime.jobs.fetch.action_name', {}, '拉回结果', 'Retrieve results');
    const dirs = actionDirs(name, actionLabel, 'bound');
    if (dirs === null) return;
    if (!dirs.length) {
      VCS.log(tr('runtime.jobs.fetch.selection_required', {},
        '请先选中要拉回结果的作业(通常是 DONE/未收敛 的)',
        'Select jobs whose results should be retrieved (usually DONE or unconverged jobs)'), 'failc');
      return;
    }
    return withExclusiveOperation('fetch', actionLabel, dirs, name, async op => {
      const choice = await askFetchFiles(dirs.length);
      if (!choice) return { status: 'cancelled' };
      updateOperation(op, 'running');
      const files = choice.files;
      VCS.log(tr('runtime.jobs.fetch.starting', {
        count: dirs.length, files: files ? files.join(', ') : choice.label,
      }, '拉回 {count} 个作业：{files}…', 'Retrieving {count} jobs: {files}…'));
      const res = await remote(name, (pw, trust) =>
        VCS.call('fetch_jobs', dirs, name, pw, trust, files));
      if (!res) return { status: 'cancelled' };
      if (res.error) {
        VCS.log(tr('runtime.jobs.fetch.failed', { error: res.error },
          '拉回异常:{error}', 'Result retrieval failed: {error}'), 'failc');
        return { status: 'failed', error: res.error, value: res };
      }
      logResults(res.results, true);
      await reload();
      return { status: 'succeeded', value: res };
    });
  }

  // ── 续算(有界恢复) ───────────────────────────────────────────────────────
  async function doContinue() {
    const name = requireProfile();
    if (!name) return;
    const actionLabel = tr('runtime.jobs.continue.action_name', {}, '续算', 'Continue');
    const dirs = actionDirs(name, actionLabel, 'bound');
    if (dirs === null) return;
    if (!dirs.length) {
      VCS.log(tr('runtime.jobs.continue.selection_required', {},
        '请先选中要续算的作业(仅未收敛/墙钟/ZBRENT 等可续算)',
        'Select jobs to continue (only unconverged, wall-time, ZBRENT, and similar recoverable jobs)'), 'failc');
      return;
    }
    return withExclusiveOperation('continue', actionLabel, dirs, name, async op => {
      const ok = await VCS.confirm(tr('runtime.jobs.continue.confirm', {
        count: dirs.length, server: name, targets: operationTargetText(dirs),
      }, '将从 CONTCAR 续算并重投以下 {count} 个作业到「{server}」：\n{targets}\n\nINCAR 冻结；每作业上限 3 轮。不可续算的会被后端跳过。\n\n继续?',
      'Continue and resubmit these {count} jobs from CONTCAR to "{server}":\n{targets}\n\nINCAR is frozen and each job is limited to 3 rounds. The backend will skip jobs that cannot be continued.\n\nContinue?'));
      if (!ok) return { status: 'cancelled' };
      updateOperation(op, 'running');
      VCS.log(tr('runtime.jobs.continue.connecting', { count: dirs.length },
        '连接并续算 {count} 个作业…', 'Connecting and continuing {count} jobs…'));
      const res = await remote(name, (pw, trust) =>
        VCS.call('continue_jobs', dirs, name, pw, trust, op.id));
      if (!res) return { status: 'cancelled' };
      if (res.error) {
        VCS.log(tr('runtime.jobs.continue.failed', { error: res.error },
          '续算异常:{error}', 'Continuation failed: {error}'), 'failc');
        return { status: 'failed', error: res.error, value: res };
      }
      logResults(res.results, true);
      await reload();
      return { status: 'succeeded', value: res };
    });
  }

  // ── 集群队列 + 认领 ────────────────────────────────────────────────────────
  async function doQueue() {
    const name = requireProfile();
    if (!name) return;
    VCS.log(tr('runtime.jobs.queue.querying', { server: name },
      '查询「{server}」上的全部队列作业…', 'Querying all queued jobs on "{server}"…'));
    const res = await remote(name, (pw, trust) => VCS.call('queue_detail', name, pw, trust));
    if (!res) return;
    if (res.error) {
      VCS.log(tr('runtime.jobs.queue.query_failed', { error: res.error },
        '队列查询异常:{error}', 'Queue query failed: {error}'), 'failc');
      return;
    }
    await showQueueModal(name, res.jobs || []);
  }

  // 作业名/号 → 安全本地目录段(与后端 batch_ops._sanitize_seg 同口径)
  function sanitizeSeg(s) {
    const seg = String(s || '').replace(/[^A-Za-z0-9_.-]/g, '_');
    return seg || '_';
  }
  // 本地根 + 作业名 → 预填本地目录(客户端以 '\\' 拼,可改)
  function joinLocal(root, name) {
    const r = String(root || '').replace(/[\\/]+$/, '');
    const seg = sanitizeSeg(name);
    return r ? r + '\\' + seg : seg;
  }

  async function showQueueModal(name, jobs) {
    const known = new Set(State.rows.filter(r => r.job_id).map(r => String(r.job_id)));
    if (!jobs.length) VCS.log(tr('runtime.jobs.queue.empty', { server: name },
      '「{server}」队列为空(该用户当前没有在队/在跑作业)',
      'The queue on "{server}" is empty (this user has no queued or running jobs)'));
    // 认领本地根目录(未配置 → 后端给默认 %USERPROFILE%\vcstudio_jobs)
    const rr = await VCS.call('adopt_root_get');
    let root = (rr && rr.root) || '';
    const unmanaged = jobs.filter(j => !known.has(String(j.job_id)));

    // 顶部头行:一键认领全部未纳入(N,为 0 时隐藏)+ 当前认领根目录 + 修改链接
    const header =
      '<div style="display:flex;justify-content:space-between;align-items:center;' +
      'gap:10px;margin-bottom:8px;flex-wrap:wrap">' +
      '<div>' + (unmanaged.length
        ? `<button class="btn primary" id="adopt-all-btn">${VCS.esc(tr(
          'runtime.jobs.queue.claim_all', { count: unmanaged.length },
          '一键认领全部未纳入({count})', 'Claim all unmanaged jobs ({count})'))}</button>`
        : '') + '</div>' +
      `<div class="sub">${VCS.esc(tr('runtime.jobs.queue.local_root', {},
        '本地根目录:', 'Local root:'))}<span id="adopt-root-val" class="mono">` +
      `${VCS.esc(root)}</span> <a href="#" id="adopt-root-edit">${VCS.esc(tr(
        'runtime.jobs.common.edit', {}, '修改', 'Edit'))}</a></div></div>`;

    const queueLabel = tr('runtime.jobs.queue.table_label', {}, '集群队列作业', 'Cluster queue jobs');
    let h = header + `<div class="table-scroll" role="region" aria-label="${VCS.esc(queueLabel)}" ` +
      'tabindex="0" style="max-height:52vh;overflow:auto">' +
      `<table aria-label="${VCS.esc(queueLabel)}"><thead><tr>` +
      `<th scope="col" class="mono">${VCS.esc(tr('runtime.jobs.table.job_id', {},
        '作业号', 'Job ID'))}</th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.table.status', {}, '状态', 'Status'))}</th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.queue.job_name', {}, '作业名', 'Job name'))}</th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.queue.remote_directory', {},
        '远程目录', 'Remote directory'))}</th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.queue.management', {}, '纳管', 'Management'))}</th>` +
      '</tr></thead><tbody>';
    jobs.forEach((j, i) => {
      const managed = known.has(String(j.job_id));
      const st = j.state === 'RUNNING'
        ? tr('runtime.jobs.queue.running', {}, '运行中', 'Running')
        : tr('runtime.jobs.queue.queued', {}, '排队中', 'Queued');
      h += `<tr data-i="${i}">` +
        `<td class="mono">${VCS.esc(j.job_id)}</td>` +
        `<td>${VCS.esc(st)}</td>` +
        `<td>${VCS.esc(j.name || '')}</td>` +
        `<td class="sub">${VCS.esc(j.workdir || '')}</td>` +
        `<td>${managed ? VCS.esc(tr('runtime.jobs.queue.managed', {}, '已纳管', 'Managed'))
          : `<button class="btn quiet" data-claim="${i}">${VCS.esc(tr(
            'runtime.jobs.queue.claim', {}, '认领', 'Claim'))}</button>`}</td></tr>`;
    });
    h += '</tbody></table></div>' +
      `<p class="sub" style="margin-top:8px">${VCS.esc(tr('runtime.jobs.queue.claim_explanation', {},
        '「认领」= 把不是本软件提交的作业(如终端手动 qsub)纳入台账,之后可查状态/拉回/续算。「一键认领」自动补远程工作目录,本地目录建在根目录下。',
        'Claim adds jobs that were not submitted by this application (for example, a manual qsub) to the ledger so their status can be queried and their results retrieved or continued. Claim All fills in remote working directories automatically and creates local directories under the configured root.'))}</p>`;
    const m = VCS.modal({
      title: tr('runtime.jobs.queue.modal_title', { server: name },
        '集群队列 — {server}', 'Cluster queue — {server}'),
      bodyHTML: h,
      actions: [{ label: tr('runtime.jobs.common.close', {}, '关闭', 'Close'),
        onClick: mm => mm.close() }],
    });

    // 修改认领根目录(prompt 式 modal → adopt_root_set)
    const editLink = m.el.querySelector('#adopt-root-edit');
    if (editLink) editLink.addEventListener('click', e => {
      e.preventDefault();
      editAdoptRoot(root, newRoot => {
        root = newRoot;
        const val = m.el.querySelector('#adopt-root-val');
        if (val) val.textContent = newRoot;
      });
    });

    // 一键认领全部未纳入
    const allBtn = m.el.querySelector('#adopt-all-btn');
    if (allBtn) allBtn.addEventListener('click', async () => {
      const ok = await VCS.confirm(
        tr('runtime.jobs.queue.claim_all_confirm', { count: unmanaged.length, root },
          '将认领 {count} 个作业,本地目录建在 {root} 下',
          'Claim {count} jobs and create their local directories under {root}?'));
      if (!ok) return;
      m.close();
      VCS.log(tr('runtime.jobs.queue.claiming_all', { count: unmanaged.length },
        '一键认领 {count} 个未纳入作业(自动补远程目录)…',
        'Claiming {count} unmanaged jobs (remote directories will be filled automatically)…'));
      const res = await remote(name, (pw, trust) => VCS.call('adopt_all', name, pw, trust));
      if (!res) return;
      if (res.error) {
        VCS.log(tr('runtime.jobs.queue.claim_all_failed', { error: res.error },
          '一键认领异常:{error}', 'Claim All failed: {error}'), 'failc');
        return;
      }
      logResults(res.results, true);
      await reload();
    });

    // 单个认领
    m.el.querySelectorAll('button[data-claim]').forEach(btn => {
      btn.addEventListener('click', () => {
        const j = jobs[+btn.dataset.claim];
        askAdopt(name, j, root, () => {
          if (btn.parentNode) btn.parentNode.textContent = tr(
            'runtime.jobs.queue.managed', {}, '已纳管', 'Managed');
        });
      });
    });
  }

  // 修改认领根目录:prompt 式 modal
  function editAdoptRoot(current, onSaved) {
    const body =
      `<div class="sub" style="margin-bottom:4px">${VCS.esc(tr(
        'runtime.jobs.adopt.root_help', {}, '认领作业时,本地目录建在此根目录下',
        'Local directories for claimed jobs are created under this root'))}</div>` +
      `<input id="ar-input" class="ipt" value="${VCS.esc(current || '')}" placeholder="${VCS.esc(tr(
        'runtime.jobs.adopt.root_placeholder', {}, '例如 E:\\runs', 'For example, E:\\runs'))}">`;
    VCS.modal({
      title: tr('runtime.jobs.adopt.root_title', {},
        '设置认领根目录', 'Set claim root directory'),
      bodyHTML: body,
      actions: [
        { label: tr('runtime.jobs.common.cancel', {}, '取消', 'Cancel'),
          quiet: true, onClick: mm => mm.close() },
        { label: tr('runtime.jobs.common.save', {}, '保存', 'Save'),
          primary: true, onClick: async mm => {
            const v = mm.el.querySelector('#ar-input').value.trim();
            if (!v) {
              VCS.toast(tr('runtime.jobs.adopt.root_required', {},
                '根目录不能为空', 'The root directory cannot be empty'), 'fail');
              return;
            }
            const r = await VCS.call('adopt_root_set', v);
            if (r && r.error) {
              VCS.log(tr('runtime.jobs.adopt.root_save_failed', { error: r.error },
                '保存根目录失败:{error}', 'Failed to save root directory: {error}'), 'failc');
              return;
            }
            mm.close();
            VCS.log(tr('runtime.jobs.adopt.root_updated', { path: v },
              '认领根目录已更新:{path}', 'Claim root directory updated: {path}'), 'okc');
            if (onSaved) onSaved(v);
          } },
      ],
    });
  }

  function askAdopt(name, j, root, onDone) {
    const localDefault = joinLocal(root, j.name || j.job_id);
    const hasWd = !!(j.workdir && j.workdir.trim());
    const remotePlaceholder = hasWd
      ? tr('runtime.jobs.adopt.remote_placeholder', {},
        '/home/<用户名>/runs', '/home/<username>/runs')
      : tr('runtime.jobs.adopt.querying', {}, '查询中…', 'Querying…');
    const body =
      `<div style="margin-bottom:10px"><div class="sub" style="margin-bottom:4px">${VCS.esc(tr(
        'runtime.jobs.adopt.remote_help', {}, '远程目录(绝对路径,以 / 开头)',
        'Remote directory (absolute path beginning with /)'))}</div>` +
      `<input id="ad-remote" class="ipt" value="${VCS.esc(j.workdir || '')}" ` +
      `placeholder="${VCS.esc(remotePlaceholder)}"></div>` +
      `<div><div class="sub" style="margin-bottom:4px">${VCS.esc(tr(
        'runtime.jobs.adopt.local_help', {}, '本地目录(结果将拉回到这里;不存在会新建)',
        'Local directory (results are retrieved here; it will be created if needed)'))}</div>` +
      `<input id="ad-local" class="ipt" value="${VCS.esc(localDefault)}" placeholder="${VCS.esc(tr(
        'runtime.jobs.adopt.local_placeholder', { job_id: j.job_id },
        '例如 E:\\runs\\claimed_{job_id}', 'For example, E:\\runs\\claimed_{job_id}'))}"></div>`;
    const m = VCS.modal({
      title: tr('runtime.jobs.adopt.modal_title', {
        job_id: j.job_id, name: j.name ? ` · ${j.name}` : '',
      }, '认领作业 {job_id}{name}', 'Claim job {job_id}{name}'),
      bodyHTML: body,
      actions: [
        { label: tr('runtime.jobs.common.cancel', {}, '取消', 'Cancel'),
          quiet: true, onClick: mm => mm.close() },
        { label: tr('runtime.jobs.queue.claim', {}, '认领', 'Claim'),
          primary: true, onClick: async mm => {
            const remoteDir = mm.el.querySelector('#ad-remote').value.trim();
            const local = mm.el.querySelector('#ad-local').value.trim();
            if (!local) {
              VCS.toast(tr('runtime.jobs.adopt.local_required', {},
                '请填写本地目录', 'Enter a local directory'), 'fail');
              return;
            }
            if (!remoteDir) {
              VCS.toast(tr('runtime.jobs.adopt.remote_required', {},
                '请填写远程目录', 'Enter a remote directory'), 'fail');
              return;
            }
            mm.close();
            const r = await VCS.call('adopt_job', local, name, String(j.job_id), remoteDir, j.name || '');
            if (r && r.error) {
              VCS.log(tr('runtime.jobs.adopt.failed', { error: r.error },
                '认领失败:{error}', 'Failed to claim job: {error}'), 'failc');
              return;
            }
            VCS.log(tr('runtime.jobs.adopt.succeeded', { job_id: j.job_id, local },
              '已认领作业 {job_id} → {local}(下次「查询状态」即可追踪)',
              'Claimed job {job_id} → {local} (track it with the next status query)'), 'okc');
            if (onDone) onDone();
            await reload();
          } },
      ],
    });
    // 远程目录为空 → 自动查询工作目录预填(占位符「查询中…」直到返回)
    if (!hasWd) {
      const inp = m.el.querySelector('#ad-remote');
      remote(name, (pw, trust) => VCS.call('query_workdir', String(j.job_id), name, pw, trust))
        .then(res => {
          if (!inp) return;
          if (res && res.workdir && !inp.value.trim()) inp.value = res.workdir;
          inp.placeholder = tr('runtime.jobs.adopt.remote_placeholder', {},
            '/home/<用户名>/runs', '/home/<username>/runs');
        });
    }
  }

  // ── 打开目录 / 移出台账 / 清理失效 ─────────────────────────────────────────
  async function doOpen() {
    const dirs = selectedDirs();
    if (!dirs.length) {
      VCS.log(tr('runtime.jobs.directory.selection_required', {},
        '请先选中一个作业再打开目录', 'Select a job before opening its directory'), 'failc');
      return;
    }
    const r = await VCS.call('open_dir', dirs[0]);
    if (r && r.error) VCS.log(tr('runtime.jobs.directory.open_failed', { error: r.error },
      '打开目录失败:{error}', 'Failed to open directory: {error}'), 'failc');
  }

  async function doRemove() {
    const dirs = selectedDirs();
    if (!dirs.length) {
      VCS.log(tr('runtime.jobs.remove.selection_required', {},
        '请先选中要移出台账的作业', 'Select jobs to remove from the ledger'), 'failc');
      return;
    }
    const actionLabel = tr('runtime.jobs.remove.action_name', {}, '移出台账', 'Remove from ledger');
    return withExclusiveOperation('remove', actionLabel, dirs, '', async op => {
      const ok = await VCS.confirm(tr('runtime.jobs.remove.confirm', { count: dirs.length },
        '把 {count} 个作业移出台账?(不删除磁盘文件)',
        'Remove {count} jobs from the ledger? Files on disk will not be deleted.'));
      if (!ok) return { status: 'cancelled' };
      updateOperation(op, 'running');
      const r = await VCS.call('remove_jobs', dirs);
      if (r && r.error) {
        VCS.log(tr('runtime.jobs.remove.failed', { error: r.error },
          '移出失败:{error}', 'Failed to remove jobs: {error}'), 'failc');
        return { status: 'failed', error: r.error, value: r };
      }
      VCS.log(tr('runtime.jobs.remove.succeeded', { count: dirs.length },
        '已移出 {count} 个作业', 'Removed {count} jobs from the ledger'), 'okc');
      await reload();
      return { status: 'succeeded', value: r };
    });
  }

  async function doClean() {
    if (!State.stale.length) {
      VCS.log(tr('runtime.jobs.stale.none', {},
        '没有失效条目,无需清理', 'There are no stale entries to clean'));
      return;
    }
    const ok = await VCS.confirm(tr('runtime.jobs.stale.clean_confirm', {
      count: State.stale.length,
    }, '将把 {count} 个失效条目(目录或 job.yaml 已不存在)移出台账。\n不删除磁盘文件。继续?',
    'Remove {count} stale entries whose directory or job.yaml no longer exists from the ledger?\nFiles on disk will not be deleted. Continue?'));
    if (!ok) return;
    const r = await VCS.call('clean_stale');
    if (r && r.error) {
      VCS.log(tr('runtime.jobs.stale.clean_failed', { error: r.error },
        '清理失败:{error}', 'Failed to clean stale entries: {error}'), 'failc');
      return;
    }
    VCS.log(tr('runtime.jobs.stale.cleaned', { count: (r && r.removed) || 0 },
      '已清理 {count} 个失效条目', 'Cleaned {count} stale entries'), 'okc');
    await reload();
  }

  // ── 自动刷新 ───────────────────────────────────────────────────────────────
  function autoTick() { runAllStatus(true); }

  // silent=true(启动装载时):只起停定时器,不写日志——启动时可见页是仪表盘,
  // 此刻 VCS.log 会给无日志区的仪表盘凭空插一个日志框,故静默。
  function applyAuto(silent) {
    const cb = $('#jb-auto');
    const sel = $('#jb-interval');
    if (State.autoTimer) { clearInterval(State.autoTimer); State.autoTimer = null; }
    if (cb && cb.checked) {
      const mins = parseInt(sel.value, 10) || 10;
      State.autoTimer = setInterval(autoTick, mins * 60000);
      if (!silent) VCS.log(tr('runtime.jobs.auto.enabled', { minutes: mins },
        '自动刷新开启(每 {minutes} 分钟)',
        'Automatic refresh enabled (every {minutes} minutes)'));
    } else if (!silent) {
      VCS.log(tr('runtime.jobs.auto.disabled', {},
        '自动刷新已关闭', 'Automatic refresh disabled'));
    }
  }

  // ── 全选/反选 + 批量取消勾选作业 ──
  function checkAll() {
    State.selectionGeneration += 1;
    const name = currentProfile();
    // 全选不会跨服务器；未提交作业仍可勾选后提交到当前服务器。
    const rows = visibleRows().filter(r => !r.cluster || r.cluster === name);
    const allSel = rows.length && rows.every(r => State.selected.has(r.dir));
    if (allSel) rows.forEach(r => State.selected.delete(r.dir));
    else rows.forEach(r => State.selected.add(r.dir));
    renderTable();
    publishCurrentJobSelection('');
  }
  async function batchCancel() {
    const name = requireProfile();
    if (!name) return;
    const actionLabel = tr('runtime.jobs.cancel.action_name', {}, '批量取消', 'Batch cancel');
    const dirs = actionDirs(name, actionLabel, 'bound');
    if (dirs === null) return;
    if (!dirs.length) {
      VCS.log(tr('runtime.jobs.cancel.selection_required', {},
        '批量取消:请先勾选要取消的作业', 'Batch cancel: select the jobs to cancel'), 'failc');
      return;
    }
    return withExclusiveOperation('cancel', actionLabel, dirs, name, async op => {
      if (!await VCS.confirm(tr('runtime.jobs.cancel.confirm', {
        count: dirs.length, server: name, targets: operationTargetText(dirs),
      }, '确认在「{server}」取消以下 {count} 个作业？\n{targets}\n\n这会执行 qdel/scancel，并在台账标记 FAILED/用户取消。',
      'Cancel these {count} jobs on "{server}"?\n{targets}\n\nThis runs qdel/scancel and marks them FAILED/user-cancelled in the ledger.'))) {
        return { status: 'cancelled' };
      }
      updateOperation(op, 'running');
      const res = await remote(name, (pw, trust) =>
        VCS.call('jobs_cancel_batch', dirs, name, pw, trust, op.id));
      if (!res) return { status: 'cancelled' };
      if (res.error) {
        VCS.log(tr('runtime.jobs.cancel.failed', { error: res.error },
          '批量取消失败:{error}', 'Batch cancellation failed: {error}'), 'failc');
        return { status: 'failed', error: res.error, value: res };
      }
      (res.cancelled || []).forEach(j => VCS.log(tr('runtime.jobs.cancel.job_cancelled', { job_id: j },
        '已取消作业号 {job_id}', 'Cancelled job ID {job_id}'), 'okc'));
      (res.failed || []).forEach(f => VCS.log(tr('runtime.jobs.cancel.job_failed', {
        job_id: f.job_id, reason: f.reason,
      }, '取消失败 {job_id}:{reason}', 'Cancellation failed for {job_id}: {reason}'), 'failc'));
      await reload();
      return { status: 'succeeded', value: res };
    });
  }

  // ── 快速批量提交:父目录扫描 → 四件套补齐 → 建作业 → 自动勾选 ──
  const QS = {
    files: [], scan: null, scanSeq: 0, scanning: false,
    preparedCount: 0, selectedCount: 0,
  };
  function qsSharedIncar() { return ($('#qs-incar') && $('#qs-incar').value.trim()) || ''; }
  function qsBuildable() {
    return (((QS.scan || {}).items) || []).filter(i => i && i.can_build).length;
  }
  function qsUpdateBuildButton() {
    const btn = $('#qs-build');
    if (!btn) return;
    const n = qsBuildable();
    btn.disabled = QS.scanning || n === 0;
    btn.textContent = QS.scanning
      ? tr('runtime.jobs.quick.scanning', {}, '正在扫描…', 'Scanning…')
      : (n ? tr('runtime.jobs.quick.build_count', { count: n },
        '生成 / 导入 {count} 个作业', 'Generate / import {count} jobs')
        : tr('runtime.jobs.quick.select_and_precheck', {},
          '请先选择并预检', 'Select inputs and run the precheck'));
  }
  function renderQsFiles() {
    const box = $('#qs-filelist');
    const cnt = $('#qs-count');
    if (cnt) cnt.textContent = tr('runtime.jobs.quick.entry_count', { count: QS.files.length },
      '{count} 个入口', '{count} entries');
    if (!box) return;
    box.innerHTML = QS.files.map((f, i) =>
      `<span class="qs-file" data-i="${i}">${VCS.esc(f)} <b data-rm="${i}">×</b></span>`).join('') ||
      `<span class="sub">${VCS.esc(tr('runtime.jobs.quick.none_selected', {},
        '尚未选择。优先选择包含多个计算目录的父文件夹',
        'Nothing selected. Prefer a parent folder that contains multiple calculation directories'))}</span>`;
  }
  function qsStatus(item) {
    if (item.status === 'ready') return ['ready', tr('runtime.jobs.quick.status_ready', {},
      '四件套完整', 'Four-file set complete')];
    if (item.status === 'generatable') return ['generatable', tr(
      'runtime.jobs.quick.status_generatable', {}, '可自动补齐', 'Can be completed automatically')];
    return ['blocked', tr('runtime.jobs.quick.status_blocked', {},
      '需要处理', 'Needs attention')];
  }
  function renderQsNext() {
    const next = $('#qs-next');
    if (!next || !QS.preparedCount) return;
    next.hidden = false;
    const title = $('#qs-next-title');
    const note = $('#qs-next-note');
    if (title) title.textContent = tr('runtime.jobs.quick.prepared_summary', {
      count: QS.preparedCount,
    }, '已准备 {count} 个作业', 'Prepared {count} jobs');
    if (note) note.textContent = tr('runtime.jobs.quick.selected_for_submit', {
      count: QS.selectedCount,
    }, '其中 {count} 个已在下方自动勾选；提交前仍会显示集群与远程目录确认。',
    '{count} jobs are selected below. The cluster and remote directory will still be confirmed before submission.');
  }
  function renderQsPreview(r) {
    const box = $('#qs-preview');
    if (!box) return;
    if (!QS.files.length) { box.hidden = true; box.innerHTML = ''; qsUpdateBuildButton(); return; }
    box.hidden = false;
    if (QS.scanning) {
      box.innerHTML = `<div class="qs-preview-head"><strong>${VCS.esc(tr(
        'runtime.jobs.quick.scanning_directories', {},
        '正在递归扫描目录并检查四件套…',
        'Recursively scanning directories and checking four-file sets…'))}</strong></div>`;
      qsUpdateBuildButton();
      return;
    }
    if (!r || r.error || r.ok === false) {
      const error = (r && r.error) || tr('runtime.jobs.quick.cannot_read_input', {},
        '无法读取输入', 'Unable to read input');
      box.innerHTML = `<div class="qs-preview-head"><strong>${VCS.esc(tr(
        'runtime.jobs.quick.precheck_failed', {}, '预检失败', 'Precheck failed'))}</strong>` +
        `<span class="qs-stat bad">${VCS.esc(tr('runtime.jobs.common.raw_message', { message: error },
          '{message}', '{message}'))}</span></div>`;
      qsUpdateBuildButton();
      return;
    }
    const s = r.summary || {};
    const items = r.items || [];
    let h = `<div class="qs-preview-head"><strong>${VCS.esc(tr(
      'runtime.jobs.quick.precheck_summary', { count: s.total || 0 },
      '预检结果：发现 {count} 个候选作业', 'Precheck result: {count} candidate jobs found'))}</strong>` +
      `<span class="qs-stat ok">${VCS.esc(tr('runtime.jobs.quick.ready_count', { count: s.ready || 0 },
        '完整 {count}', 'Complete: {count}'))}</span>` +
      `<span class="qs-stat gen">${VCS.esc(tr('runtime.jobs.quick.generatable_count', {
        count: s.generatable || 0,
      }, '可生成 {count}', 'Can generate: {count}'))}</span>` +
      `<span class="qs-stat bad">${VCS.esc(tr('runtime.jobs.quick.blocked_count', { count: s.blocked || 0 },
        '需处理 {count}', 'Needs attention: {count}'))}</span>` +
      ((s.duplicates || 0) ? `<span class="qs-stat">${VCS.esc(tr(
        'runtime.jobs.quick.duplicates_count', { count: s.duplicates },
        '已去重 {count}', 'Deduplicated: {count}'))}</span>` : '') +
      '</div><div class="qs-preview-list">';
    if (!items.length) {
      h += `<div class="empty"><p>${VCS.esc(tr('runtime.jobs.quick.no_usable_input', {},
        '没有发现可用输入。请选择包含 POSCAR、CONTCAR 或四件套的目录。',
        'No usable input found. Select a directory containing POSCAR, CONTCAR, or a complete four-file set.'))}</p></div>`;
    } else {
      items.forEach(item => {
        const st = qsStatus(item);
        const missing = item.missing || [];
        h += '<div class="qs-preview-row">' +
          `<div class="qs-preview-name"><b>${VCS.esc(item.name || tr(
            'runtime.jobs.quick.unnamed', {}, '未命名', 'Unnamed'))}</b>` +
          `<span title="${VCS.esc(item.path || '')}">${VCS.esc(item.path || '')}</span></div>` +
          `<div><span class="qs-status ${st[0]}">${VCS.esc(st[1])}</span>` +
          (missing.length
            ? `<span class="qs-missing${item.can_build ? '' : ' bad'}">${VCS.esc(tr(
              'runtime.jobs.quick.missing_files', {
                files: missing.join(tr('runtime.jobs.common.list_separator', {}, '、', ', ')),
              }, '缺：{files}', 'Missing: {files}'))}</span>`
            : `<span class="qs-missing">${VCS.esc(tr('runtime.jobs.quick.files_complete', {},
              '文件完整', 'Files complete'))}</span>`) + '</div>' +
          `<div class="qs-preview-msg">${VCS.esc(tr('runtime.jobs.common.raw_message', {
            message: item.message || '',
          }, '{message}', '{message}'))}</div></div>`;
      });
    }
    h += '</div>';
    box.innerHTML = h;
    qsUpdateBuildButton();
  }
  async function qsScan() {
    const seq = ++QS.scanSeq;
    if (!QS.files.length) {
      QS.scan = null; QS.scanning = false; renderQsPreview(null); return null;
    }
    QS.scanning = true;
    const box = $('#qs-preview');
    if (box) {
      box.hidden = false;
      box.innerHTML = `<div class="qs-preview-head"><strong>${VCS.esc(tr(
        'runtime.jobs.quick.scanning_directories', {},
        '正在递归扫描目录并检查四件套…',
        'Recursively scanning directories and checking four-file sets…'))}</strong></div>`;
    }
    qsUpdateBuildButton();
    let r;
    try {
      r = await VCS.call('quick_submit_scan', QS.files, qsSharedIncar());
    } catch (e) {
      r = { ok: false, error: String(e), items: [], summary: {} };
    }
    if (seq !== QS.scanSeq) return null;
    QS.scanning = false;
    QS.scan = r;
    renderQsPreview(r);
    return r;
  }
  function qsSuggestOut(path, isDir) {
    const out = $('#qs-out');
    if (!out || out.value.trim() || !path) return;
    const clean = String(path).replace(/[\\/]+$/, '');
    if (isDir) out.value = clean + '_vcstudio_jobs';
    else {
      const m = clean.match(/^(.*)[\\/][^\\/]+$/);
      out.value = (m ? m[1] + clean.slice(m[1].length, m[1].length + 1) : '') + 'vcstudio_jobs';
    }
  }
  async function qsAdd() {
    const r = await VCS.call('pick_file', 'input');
    if (r && r.error) {
      VCS.log(tr('runtime.jobs.quick.pick_file_failed', { error: r.error },
        '选择文件失败:{error}', 'Failed to select file: {error}'), 'failc');
      return;
    }
    if (r && r.path && QS.files.indexOf(r.path) < 0) {
      QS.files.push(r.path); renderQsFiles(); qsSuggestOut(r.path, false); await qsScan();
    }
  }
  async function qsAddVaspDir() {
    const r = await VCS.call('pick_dir');
    if (r && r.error) {
      VCS.log(tr('runtime.jobs.quick.pick_directory_failed', { error: r.error },
        '选择目录失败:{error}', 'Failed to select directory: {error}'), 'failc');
      return;
    }
    if (r && r.path && QS.files.indexOf(r.path) < 0) {
      QS.files.push(r.path);
      renderQsFiles();
      qsSuggestOut(r.path, true);
      VCS.log(tr('runtime.jobs.quick.scanning_parent', { path: r.path },
        '正在扫描父目录:{path}', 'Scanning parent directory: {path}'));
      const scan = await qsScan();
      if (scan && scan.ok) {
        const s = scan.summary || {};
        VCS.log(tr('runtime.jobs.quick.scan_complete', {
          total: s.total || 0, ready: s.ready || 0,
          generatable: s.generatable || 0, blocked: s.blocked || 0,
        }, '已发现 {total} 个候选作业：完整 {ready}，可自动补齐 {generatable}，需处理 {blocked}',
        'Found {total} candidate jobs: {ready} complete, {generatable} can be completed automatically, {blocked} need attention'), 'okc');
      }
    }
  }
  async function qsBuild() {
    if (!QS.files.length) {
      VCS.log(tr('runtime.jobs.quick.input_required', {},
        '快速提交:请先添加 VASP 目录或输入文件',
        'Quick submit: add a VASP directory or input file first'), 'failc');
      return;
    }
    if (!QS.scan || QS.scanning) await qsScan();
    if (!qsBuildable()) {
      VCS.log(tr('runtime.jobs.quick.none_buildable', {},
        '没有可建作业。若只有 POSCAR/CONTCAR，请先选择共享 INCAR；并检查预检中的缺项。',
        'No jobs can be built. If only POSCAR/CONTCAR is available, select a shared INCAR and review the missing files reported by the precheck.'), 'failc');
      return;
    }
    const out = ($('#qs-out') && $('#qs-out').value.trim()) || '';
    if (!out) {
      VCS.log(tr('runtime.jobs.quick.output_required', {},
        '快速提交:请选择输出根目录', 'Quick submit: select an output root directory'), 'failc');
      return;
    }
    const shared = qsSharedIncar();
    const lib = ($('#qs-lib') && $('#qs-lib').value.trim()) || '';
    const calcType = ($('#qs-calc-type') && $('#qs-calc-type').value) || 'slab';
    VCS.log(tr('runtime.jobs.quick.building', { count: qsBuildable() },
      '正在生成 / 导入 {count} 个作业；源文件保持不变…',
      'Generating / importing {count} jobs; source files will remain unchanged…'));
    const r = await VCS.call(
      'quick_submit_build', QS.files, out, '', shared, lib, calcType);
    if (!r || r.ok === false || r.error) {
      VCS.log(tr('runtime.jobs.quick.build_failed', {
        error: (r && r.error) || tr('runtime.jobs.common.unknown_error', {},
          '未知错误', 'Unknown error'),
      }, '快速提交失败:{error}', 'Quick submit failed: {error}'), 'failc'); return;
    }
    (r.jobs || []).forEach(j => {
      VCS.log(tr('runtime.jobs.quick.job_built', {
        name: j.name, engine: j.engine, path: j.dir,
      }, '已建作业:{name}({engine})→ {path}', 'Built job: {name} ({engine}) → {path}'), 'okc');
      if (j.hint) VCS.log(tr('runtime.jobs.quick.submit_hint', { message: j.hint },
        '  提交命令模板:{message}', '  Submission command template: {message}'));
    });
    (r.skipped || []).forEach(s => VCS.log(tr('runtime.jobs.quick.skipped', {
      file: s.file, reason: s.reason,
    }, '跳过 {file}:{reason}', 'Skipped {file}: {reason}'), 'warnc'));
    const made = r.jobs || [];
    VCS.log(tr('runtime.jobs.quick.built_summary', { count: made.length },
      '已建 {count} 个作业并入台账', 'Built and added {count} jobs to the ledger'), 'okc');
    VCS.toast(tr('runtime.jobs.quick.prepared_summary', { count: made.length },
      '已准备 {count} 个作业', 'Prepared {count} jobs'));
    await reload();
    // 新作业自动勾选；用户仍能在真正远程提交的确认框前增删选择。
    const present = new Set(State.rows.map(row => row.dir));
    let selected = 0;
    made.forEach(job => {
      if (present.has(job.dir)) { State.selected.add(job.dir); selected += 1; }
    });
    renderTable();
    const next = $('#qs-next');
    if (next && made.length) {
      QS.preparedCount = made.length;
      QS.selectedCount = selected;
      renderQsNext();
      next.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  // ── 文件管理:列远端目录 + 下载选中 ──
  const FM = { entries: [], checked: new Set(), path: '', loaded: false };
  function fmClusters() {
    const sel = $('#fm-cluster');
    if (!sel) return;
    const previous = sel.value;
    const names = Object.keys(State.profiles);
    sel.innerHTML = names.length
      ? names.map(n => `<option value="${VCS.esc(n)}">${VCS.esc(n)}</option>`).join('')
      : `<option value="">${VCS.esc(tr('runtime.jobs.profile.none_configured', {},
        '(未配置集群)', '(No clusters configured)'))}</option>`;
    if (previous && names.indexOf(previous) >= 0) sel.value = previous;
  }
  async function fmList() {
    const name = $('#fm-cluster') ? $('#fm-cluster').value : '';
    if (!name || !State.profiles[name]) {
      VCS.log(tr('runtime.jobs.files.cluster_required', {},
        '文件管理:请先选集群', 'File manager: select a cluster first'), 'failc');
      return;
    }
    const path = ($('#fm-path') && $('#fm-path').value.trim()) || '.';
    VCS.log(tr('runtime.jobs.files.listing', { server: name, path },
      '列远端目录:{server}:{path} …', 'Listing remote directory: {server}:{path} …'));
    const res = await remote(name, (pw, trust) => VCS.call('remote_ls', name, pw, path, trust));
    if (!res) return;
    if (res.error) {
      VCS.log(tr('runtime.jobs.files.list_failed', { error: res.error },
        '列目录失败:{error}', 'Failed to list directory: {error}'), 'failc');
      return;
    }
    FM.entries = res.entries || [];
    FM.checked = new Set();
    FM.path = res.path || path;
    FM.loaded = true;
    renderFmTable(FM.path);
    VCS.log(tr('runtime.jobs.files.listed_count', { count: FM.entries.length },
      '已列出 {count} 项', 'Listed {count} entries'), 'okc');
  }
  function renderFmTable(path) {
    const box = ensureFileScrollRegion();
    if (!box) return;
    if (!FM.entries.length) {
      box.innerHTML = `<span class="sub">${VCS.esc(tr('runtime.jobs.files.empty_directory', { path },
        '目录为空:{path}', 'Directory is empty: {path}'))}</span>`;
      return;
    }
    let h = `<table class="fm-tbl" aria-label="${VCS.esc(tr(
      'runtime.jobs.files.table_label', {}, '远程文件', 'Remote files'))}"><thead><tr>` +
      `<th scope="col" aria-label="${VCS.esc(tr('runtime.jobs.files.select_column', {},
        '选择文件', 'Select files'))}"></th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.files.name', {}, '名称', 'Name'))}</th>` +
      `<th scope="col" class="num">${VCS.esc(tr('runtime.jobs.files.size', {}, '大小', 'Size'))}</th>` +
      `<th scope="col">${VCS.esc(tr('runtime.jobs.files.modified', {},
        '修改时间', 'Modified'))}</th></tr></thead><tbody>`;
    FM.entries.forEach((e, i) => {
      const locale = VCS.i18n && VCS.i18n.lang === 'en' ? 'en-US' : 'zh-CN';
      const dt = e.mtime ? new Date(e.mtime * 1000).toLocaleString(locale) : '';
      const icon = e.is_dir ? '📁 ' : '';
      h += `<tr data-fi="${i}"><td>${e.is_dir ? '' : `<input type="checkbox" class="fm-chk" ` +
        `data-i="${i}" aria-label="${VCS.esc(tr('runtime.jobs.files.select_named', { name: e.name },
          '选择文件 {name}', 'Select file {name}'))}"${FM.checked.has(i) ? ' checked' : ''}>`}</td>` +
        `<td>${icon}${VCS.esc(e.name)}</td><td class="num">${e.is_dir ? '—' : fmSize(e.size)}</td>` +
        `<td class="mono">${VCS.esc(dt)}</td></tr>`;
    });
    box.innerHTML = h + '</tbody></table>';
  }
  function fmSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }
  async function fmFetch() {
    const name = $('#fm-cluster') ? $('#fm-cluster').value : '';
    if (!name) {
      VCS.log(tr('runtime.jobs.files.cluster_required', {},
        '文件管理:请先选集群', 'File manager: select a cluster first'), 'failc');
      return;
    }
    const localDir = ($('#fm-localdir') && $('#fm-localdir').value.trim()) || '';
    if (!localDir) {
      VCS.log(tr('runtime.jobs.files.local_directory_required', {},
        '文件管理:请选择本地保存目录', 'File manager: select a local save directory'), 'failc');
      return;
    }
    const picks = Array.from(FM.checked);
    if (!picks.length) {
      VCS.log(tr('runtime.jobs.files.selection_required', {},
        '文件管理:请勾选要下载的文件', 'File manager: select files to download'), 'failc');
      return;
    }
    const base0 = ($('#fm-path') && $('#fm-path').value.trim()) || '.';
    for (const i of picks) {
      const e = FM.entries[i];
      if (!e) continue;
      const rpath = base0.replace(/\/+$/, '') + '/' + e.name;
      const res = await remote(name, (pw, trust) =>
        VCS.call('remote_fetch_file', name, pw, rpath, localDir, trust));
      if (!res) return;
      if (res.error) VCS.log(tr('runtime.jobs.files.download_failed', {
        name: e.name, error: res.error,
      }, '下载失败 {name}:{error}', 'Failed to download {name}: {error}'), 'failc');
      else VCS.log(tr('runtime.jobs.files.downloaded', { path: res.local_path },
        '已下载:{path}', 'Downloaded: {path}'), 'okc');
    }
    VCS.toast(tr('runtime.jobs.files.download_complete', {},
      '下载完成', 'Download complete'));
    VCS.call('open_dir', localDir);
  }

  // ── 初始化 ─────────────────────────────────────────────────────────────────
  // ── v3.3.0 实时能量曲线:本地 OSZICAR 优先,远端 SSH 轮询(对齐 starpivot 任务监控) ──
  const Live = {
    dir: null, name: '', cluster: '', pw: null, timer: null,
    chart: null, busy: false, lastResponse: null,
  };
  function liveStop(hide) {
    if (Live.timer) { clearInterval(Live.timer); Live.timer = null; }
    if (hide) { const p = $('#jobs-live'); if (p) p.hidden = true; Live.dir = null; }
  }
  function liveSchedule() {
    if (Live.timer) { clearInterval(Live.timer); Live.timer = null; }
    const sec = parseInt(($('#jl-interval') && $('#jl-interval').value) || '30', 10);
    if (sec > 0 && Live.dir) Live.timer = setInterval(() => livePoll(), sec * 1000);
  }
  async function liveOpen(dir, jobName, cluster) {
    liveStop(false);
    Live.dir = dir; Live.name = jobName || base(dir); Live.cluster = cluster || ''; Live.pw = null;
    const p = $('#jobs-live'); if (p) p.hidden = false;
    const t = $('#jl-title');
    if (t) t.textContent = Live.name + (cluster ? '(' + cluster + ')' : '');
    const note = $('#jl-note'); if (note) note.textContent = tr(
      'runtime.jobs.live.loading', {}, '读取中…', 'Loading…');
    let res;
    if (cluster && State.profiles[cluster]) {
      // 首次经 remote 壳走密码/信任流;捕获实际使用的密码供后续静默轮询
      res = await remote(cluster, (pw, trust) => {
        Live.pw = pw;
        return VCS.call('job_live_energy', dir, cluster, pw, trust);
      });
      if (res === null) { liveStop(true); return; }
    } else {
      res = await VCS.call('job_live_energy', dir, null, null, false);
    }
    liveRender(res);
    liveSchedule();
  }
  async function livePoll() {
    if (!Live.dir || Live.busy) return;                  // 上一轮未返回则跳过本轮
    Live.busy = true;
    try {
      const res = await VCS.call('job_live_energy', Live.dir, Live.cluster || null, Live.pw, false);
      liveRender(res);
    } finally { Live.busy = false; }
  }
  function liveRender(res) {
    Live.lastResponse = res || null;
    const note = $('#jl-note');
    if (!res || res.ok === false || res.error) {
      if (note) note.textContent = tr('runtime.jobs.live.read_failed', {
        error: (res && res.error) || tr('runtime.jobs.live.read_failed_default', {},
          '读取失败', 'Read failed'),
      }, '⚠ {error}', 'WARNING: {error}');
      return;
    }
    const steps = res.steps || [];
    if (!steps.length) {
      if (note) note.textContent = tr('runtime.jobs.live.no_ionic_steps', {},
        '尚无离子步(SCF 进行中或刚启动),轮询将自动更新',
        'No ionic steps yet (SCF is in progress or has just started); polling will update automatically');
      return;
    }
    const last = steps[steps.length - 1];
    if (note) {
      note.textContent = tr('runtime.jobs.live.summary', {
        source: res.source === 'remote'
          ? tr('runtime.jobs.live.remote_oszicar', {}, '远端 OSZICAR', 'Remote OSZICAR')
          : tr('runtime.jobs.live.local_oszicar', {}, '本地 OSZICAR', 'Local OSZICAR'),
        count: steps.length,
        energy: last.e0,
        delta: last.de != null ? ` · |ΔE|=${last.de} eV` : '',
        state: res.state ? tr('runtime.jobs.live.state_suffix', { state: res.state },
          ' · 状态 {state}', ' · Status {state}') : '',
        mode: Live.timer ? '' : tr('runtime.jobs.live.manual_mode', {},
          '(手动刷新模式)', ' (manual refresh mode)'),
      }, '{source} · {count} 离子步 · 末步 E0={energy} eV{delta}{state}{mode}',
      '{source} · {count} ionic steps · latest E0={energy} eV{delta}{state}{mode}');
    }
    const box = document.getElementById('jl-chart');
    if (!box) return;
    if (typeof echarts === 'undefined') {
      box.textContent = tr('runtime.jobs.live.echarts_unavailable', {},
        '(echarts 未加载,无法画曲线)', '(echarts is not loaded; the curve cannot be plotted)');
      return;
    }
    if (!Live.chart) Live.chart = echarts.init(box);
    Live.chart.setOption({
      grid: { left: 74, right: 20, top: 24, bottom: 40 },
      xAxis: { type: 'category', name: tr('runtime.jobs.live.ionic_step_axis', {},
        '离子步', 'Ionic step'), data: steps.map(s => s.n) },
      yAxis: { type: 'value', name: 'E0 (eV)', scale: true },
      tooltip: { trigger: 'axis' },
      series: [{ type: 'line', symbolSize: 5, data: steps.map(s => s.e0) }],
    }, true);
  }

  function wire(id, fn) { const el = $('#' + id); if (el) el.addEventListener('click', fn); }

  function init() {
    ensureTableScrollRegion();
    ensureFileScrollRegion();
    wire('jb-submit', doSubmit);
    wire('jb-status', () => runStatus(false));
    wire('jb-status-all', () => runAllStatus(false));
    wire('jb-fetch', doFetch);
    wire('jb-continue', doContinue);
    wire('jb-spin', doSpinCompare);
    wire('jb-queue', doQueue);
    wire('jb-open', doOpen);
    wire('jb-report', doReport);
    wire('jb-remove', doRemove);
    wire('jb-clean', doClean);
    wire('jb-checkall', checkAll);
    wire('jb-cancelchecked', batchCancel);
    wire('jb-tray-submit', doSubmit);
    wire('jb-tray-fetch', doFetch);
    wire('jb-tray-continue', doContinue);
    wire('jb-tray-cancel', batchCancel);
    wire('jb-tray-remove', doRemove);
    wire('jb-tray-forecast', estimateSelectedResources);
    wire('jobs-selection-clear', clearSelection);
    wire('jobs-selection-review', () => {
      State.selectionTrayExpanded = !State.selectionTrayExpanded;
      renderSelectionTray();
    });
    // 实时能量曲线面板(v3.3.0)
    wire('jl-refresh', () => livePoll());
    wire('jl-close', () => liveStop(true));
    { const el = $('#jl-interval'); if (el) el.addEventListener('change', liveSchedule); }
    // 筛选行:变更即重渲
    ['jf-cluster', 'jf-status', 'jf-sort'].forEach(id => {
      const el = $('#' + id);
      if (el) el.addEventListener('change', renderTable);
    });
    // 快速批量提交
    wire('qs-add', qsAdd);
    wire('qs-add-dir', qsAddVaspDir);
    wire('qs-clearfiles', () => {
      QS.files = []; QS.scan = null; QS.scanSeq += 1; QS.scanning = false;
      QS.preparedCount = 0; QS.selectedCount = 0;
      renderQsFiles(); renderQsPreview(null);
      const next = $('#qs-next'); if (next) next.hidden = true;
    });
    wire('qs-incar-btn', async () => {
      const r = await VCS.call('pick_file', 'incar');
      if (r && r.error) {
        VCS.log(tr('runtime.jobs.quick.pick_incar_failed', { error: r.error },
          '选择 INCAR 失败:{error}', 'Failed to select INCAR: {error}'), 'failc');
        return;
      }
      if (r && r.path && $('#qs-incar')) {
        $('#qs-incar').value = r.path;
        VCS.log(tr('runtime.jobs.quick.shared_incar_selected', {},
          '已选择共享 INCAR，重新检查可自动补齐的结构…',
          'Shared INCAR selected; rechecking structures that can be completed automatically…'));
        await qsScan();
      }
    });
    wire('qs-lib-btn', async () => {
      const r = await VCS.call('pick_dir');
      if (r && r.error) {
        VCS.log(tr('runtime.jobs.quick.pick_potential_library_failed', { error: r.error },
          '选择赝势库失败:{error}', 'Failed to select potential library: {error}'), 'failc');
        return;
      }
      if (r && r.path && $('#qs-lib')) $('#qs-lib').value = r.path;
    });
    wire('qs-out-btn', async () => {
      const r = await VCS.call('pick_dir');
      if (r && r.path && $('#qs-out')) $('#qs-out').value = r.path;
    });
    wire('qs-build', qsBuild);
    wire('qs-go-submit', doSubmit);
    const qsi = $('#qs-incar');
    if (qsi) qsi.addEventListener('change', qsScan);
    const qsl = $('#qs-filelist');
    if (qsl) qsl.addEventListener('click', e => {
      const rm = e.target.closest('[data-rm]');
      if (rm) {
        QS.files.splice(parseInt(rm.dataset.rm, 10), 1);
        renderQsFiles(); qsScan();
      }
    });
    renderQsFiles();
    qsUpdateBuildButton();
    // 文件管理
    wire('fm-ls', fmList);
    wire('fm-fetch', fmFetch);
    wire('fm-localbtn', async () => {
      const r = await VCS.call('pick_dir');
      if (r && r.path && $('#fm-localdir')) $('#fm-localdir').value = r.path;
    });
    const fmt = $('#fm-table');
    if (fmt) fmt.addEventListener('change', e => {
      const cb = e.target.closest('.fm-chk');
      if (!cb) return;
      const i = parseInt(cb.dataset.i, 10);
      if (cb.checked) FM.checked.add(i); else FM.checked.delete(i);
    });
    const cb = $('#jb-auto');
    const iv = $('#jb-interval');
    // 自动刷新默认开 + 状态持久化(localStorage);启动即按持久化状态装载定时器
    if (iv) {
      const savedInt = localStorage.getItem(INT_KEY);
      if (savedInt) iv.value = savedInt;
      iv.addEventListener('change', () => {
        localStorage.setItem(INT_KEY, iv.value);
        if (cb && cb.checked) applyAuto();
      });
    }
    if (cb) {
      const savedAuto = localStorage.getItem(AUTO_KEY);
      cb.checked = savedAuto === null ? true : savedAuto === '1';   // 默认开
      cb.addEventListener('change', () => {
        localStorage.setItem(AUTO_KEY, cb.checked ? '1' : '0');
        applyAuto();
      });
      applyAuto(true);                               // 启动静默装载定时器(不写日志)
    }
    const psel = $('#jobs-profile');
    if (psel) psel.addEventListener('change', () => {
      const name = currentProfile();
      localStorage.setItem(PROFILE_KEY, name);
      let removed = 0;
      State.selected.forEach(d => {
        const row = State.rows.find(r => r.dir === d);
        if (row && row.cluster && row.cluster !== name) {
          State.selected.delete(d); removed++;
        }
      });
      if (removed) VCS.log(tr('runtime.jobs.profile.selection_pruned', { count: removed },
        '切换服务器后已取消 {count} 个其它服务器作业的勾选',
        'Switching servers deselected {count} jobs belonging to other servers'), 'warnc');
      renderTable();
      if (removed) publishCurrentJobSelection('');
    });
    bindTable();
    reload();
  }

  // 切回作业页时刷新台账；离开任务页会使仍在等待 reload 的旧选择失效。
  document.addEventListener('vcs:page', e => {
    if (!e.detail || e.detail.overlay) return;
    if (e.detail.page === 'jobs') reload();
    else State.selectionGeneration += 1;
  });
  function invalidateJobSelectionForProject(event) {
    State.selectionGeneration += 1;
    const detail = (event && event.detail) || {};
    const projectId = String(detail.project_id || '').trim();
    const selectedRows = Array.from(State.selected)
      .map(dir => State.rows.find(row => row.dir === dir)).filter(Boolean);
    const belongsToProject = projectId && selectedRows.length && selectedRows.every(row =>
      String(row.project_id || '').trim() === projectId);
    if (!belongsToProject) {
      State.selected.clear();
      renderTable();
      publishJobContextCleared();
    }
  }
  document.addEventListener('vcs:workspace-project', invalidateJobSelectionForProject);
  // Project 业务页会在壳层事件之前发布稳定项目上下文；这是旧调用路径的兜底。
  document.addEventListener('vcs:project-context', invalidateJobSelectionForProject);

  document.addEventListener('vcs:language', () => {
    ensureTableScrollRegion();
    ensureFileScrollRegion();
    const profile = $('#jobs-profile');
    if (profile && !Object.keys(State.profiles).length && profile.options.length) {
      profile.options[0].textContent = tr('runtime.jobs.profile.none_configured', {},
        '(未配置集群)', '(No clusters configured)');
    }
    refreshClusterFilter();
    fmClusters();
    renderTable();
    renderStats();
    renderStale();
    renderOperationQueue();
    renderResourceForecast();
    renderQsFiles();
    renderQsPreview(QS.scan);
    renderQsNext();
    if (FM.loaded) renderFmTable(FM.path);
    if (Live.lastResponse) liveRender(Live.lastResponse);
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // 结果导入可能带来只有完整四件套、尚未运行的 CREATED 成员。
  // 进入任务页时按 opaque project_id 精确展开并勾选。
  async function selectCreatedProject(projectId) {
    const selectionGeneration = ++State.selectionGeneration;
    await reload();
    if (selectionGeneration !== State.selectionGeneration) return 0;
    const wanted = String(projectId || '').trim();
    const rows = State.rows.filter(row => row.state === 'CREATED' && !row.cluster &&
      String(row.project_id || '') === wanted);
    rows.forEach(row => State.selected.add(row.dir));
    if (wanted) State.expanded.add('p:id:' + wanted);
    ['jf-cluster', 'jf-status'].forEach(id => {
      const filter = $('#' + id);
      if (filter && filter.value) filter.value = '';
    });
    renderTable();
    if (rows.length) VCS.toast(tr('runtime.jobs.submit.created_selected', { count: rows.length },
      '已勾选 {count} 个待提交作业；请选择服务器后提交',
      '{count} pending jobs selected; choose a server and submit them'));
    return rows.length;
  }

  async function selectById(id) {
    const wanted = String(id === null || id === undefined ? '' : id).trim();
    if (!wanted) return false;
    const selectionGeneration = ++State.selectionGeneration;
    let row = State.rows.find(item => stableJobId(item) === wanted) || null;
    if (!row) {
      await reload();
      if (selectionGeneration !== State.selectionGeneration) return false;
      row = State.rows.find(item => stableJobId(item) === wanted) || null;
    }
    if (selectionGeneration !== State.selectionGeneration) return false;
    if (!row) return false;
    if (!jobBelongsToWorkspaceProject(row) || !revealSelectedJob(row)) {
      publishJobContextCleared();
      return false;
    }

    State.selected.clear();
    State.selected.add(row.dir);
    if (row.project && row.project_id) {
      State.expanded.add('p:id:' + String(row.project_id));
    } else {
      State.expanded.add('s:_single');
    }
    renderTable();
    const card = ensureTableScrollRegion();
    const target = card && Array.from(card.querySelectorAll('tr[data-dir]'))
      .find(tr => tr.dataset.dir === String(row.dir || ''));
    if (target) {
      if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      }
      if (typeof target.focus === 'function') {
        try { target.focus({ preventScroll: true }); } catch (_) { target.focus(); }
      }
    }
    publishJobContext(row);
    return true;
  }

  function clearSelection() {
    // 取消跨项目切换时，连同仍在等待 reload 的旧选择和批量作用域一起作废。
    State.selectionGeneration += 1;
    const hadSelection = State.selected.size > 0;
    State.selected.clear();
    renderTable();
    publishJobContextCleared();
    return hadSelection;
  }

  // 供集群页保存与项目导入流程调用。测试 seam 仅在显式 __VCS_TEST__ 环境暴露，
  // 让 Node fake-DOM 回归执行真实状态机而不扩大生产桥接口。
  const publicJobs = { reload, selectCreatedProject, selectById, clearSelection };
  if (window.__VCS_TEST__) {
    publicJobs.__test = {
      State, performReload, renderSelectionTray, renderOperationQueue,
      renderResourceForecast, estimateSelectedResources, invalidateResourceForecast,
      withExclusiveOperation, operationTargetRows, operationTargetText,
    };
  }
  window.Jobs = publicJobs;
})();
