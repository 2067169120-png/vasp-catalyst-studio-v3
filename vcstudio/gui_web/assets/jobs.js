// jobs.js — 作业页:台账表 + 全部批量动作 + 密码/信任流 + 自动刷新 + 集群队列/认领。
// 只依赖 app.js 暴露的 VCS.* 与 api 桥方法。行为逐条对齐 vcstudio/gui/jobs_tab.py。
// 全部数据插值走 VCS.esc(注入防御);零 emoji;中文文案。
'use strict';
(function () {
  const PROFILE_KEY = 'vcs.jobs.profile';
  const AUTO_KEY = 'vcs.jobs.auto';         // 自动刷新开关(持久化;默认开)
  const INT_KEY = 'vcs.jobs.interval';      // 自动刷新间隔(分钟,持久化)
  const $ = sel => document.querySelector(sel);

  const State = {
    rows: [],            // list_jobs 返回的行
    stale: [],           // 失效条目目录
    profiles: {},        // name -> profile dict
    selected: new Set(), // 选中的 dir
    expanded: new Set(), // 展开的项目组 key(默认全部折叠;本会话内记忆)
    refreshing: false,   // 查询在飞(auto 跳过本轮的护栏)
    autoTimer: null,
    reloadGeneration: 0,
    reloadInFlight: null,
    selectionGeneration: 0,
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
    // canonical project_id 必须优先于裸 project_uuid；workspace 路由只接受前者。
    const projectId = String(row.project_id || row.project_uuid || '').trim();
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
    const rowProjectId = String(row && (row.project_id || row.project_uuid) || '').trim();
    return !!rowProjectId && rowProjectId === currentProjectId;
  }

  function ensureTableScrollRegion() {
    const card = $('#jobs-card');
    if (!card) return null;
    card.classList.add('table-scroll', 'jobs-table-scroll-region');
    card.setAttribute('role', 'region');
    card.setAttribute('aria-label', '作业列表，可水平滚动');
    card.setAttribute('tabindex', '0');
    return card;
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
        VCS.log(`${action}仅适用于尚未提交的 CREATED 作业；已绑定/已提交的作业请走续算流程：` +
          invalid.map(r => r.name).join('、'), 'failc');
        return null;
      }
    } else {
      invalid = rows.filter(r => r.cluster !== name);
      if (invalid.length) {
        VCS.log(`${action}已阻止：所选作业不属于当前服务器「${name}」：` +
          invalid.map(r => `${r.name}(${r.cluster || '尚未提交'})`).join('、'), 'failc');
        return null;
      }
    }
    return rows.map(r => r.dir);
  }

  function requireProfile() {
    const name = currentProfile();
    if (!name || !State.profiles[name]) {
      VCS.log('请先在「集群」页配置并保存一个集群,再在上方选择目标集群', 'failc');
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
      o.value = ''; o.textContent = '(未配置集群)';
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
    State.rows = (r && r.jobs) || [];
    State.stale = (r && r.stale) || [];
    if (r && r.error) VCS.log('读取台账失败:' + r.error, 'failc');
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
  const ROLE_LABEL = {
    clean: '清洁表面', gas: '气相参考', config: '吸附构型',
    molecule: '物种参考态',
  };

  // 单行作业 HTML。grpKey 非空 → 属于某折叠组(data-grp);hidden → 组当前折叠
  function rowHtml(r, grpKey, hidden) {
    const task = r.task && r.task !== '/' ? r.task : '';
    const checked = State.selected.has(r.dir);
    const sel = checked ? ' class="sel"' : '';
    const role = ROLE_LABEL[r.role] || '';
    const isQuick = String(r.task || '').startsWith('quick');
    const jobId = stableJobId(r);
    return `<tr data-dir="${VCS.esc(r.dir)}" data-name="${VCS.esc(r.name)}"` +
      (jobId ? ` data-job-id="${VCS.esc(jobId)}"` : '') +
      (grpKey ? ` data-grp="${VCS.esc(grpKey)}"` : '') +
      (hidden ? ' hidden' : '') + `${sel} tabindex="-1" aria-selected="${checked ? 'true' : 'false'}">` +
      `<td class="chk"><input type="checkbox" class="jrow-chk"${checked ? ' checked' : ''}></td>` +
      `<td>${VCS.elementBadge(r.name)}<span class="name">${VCS.esc(r.name)}</span>` +
      (role ? ` <span class="role-tag">${VCS.esc(role)}</span>` : '') +
      (r.cluster ? ` <span class="role-tag" title="该作业绑定的服务器">${VCS.esc(r.cluster)}</span>` : '') +
      (task ? ` <span class="sub">${VCS.esc(task)}</span>` : '') +
      ` <button class="lnk conv" title="查看收敛过程(E0/ΔE/|F|max vs 离子步)">收敛</button>` +
      (['RUNNING', 'QUEUED', 'SUBMITTED', 'UPLOADED'].indexOf(r.state) >= 0
        ? `<button class="lnk live" title="实时能量曲线:本地无 OSZICAR 时经 SSH 读远端,可轮询">实时</button>` : '') +
      `<button class="lnk struct" title="3D 结构预览(CONTCAR 优先,自动检查分子-衬底距离)">结构</button>` +
      `<button class="lnk meth" title="生成中英双语 Methods 段 + BibTeX(读真实 INCAR/KPOINTS/POTCAR)">方法</button>` +
      `<button class="lnk dos" title="总 DOS 出图(需本地 vasprun.xml)">DOS</button>` +
      (isQuick
        ? `<button class="lnk localrun" title="在本机跑该作业(需设置页配置本地软件命令)">本机运行</button>` : '') +
      (r.state === 'DONE'
        ? `<button class="lnk derive" title="派生频率(ZPE)/电子结构静态/AIMD 作业">派生</button>` : '') +
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
    const allRows = State.rows.filter(row => group.path
      ? row.project_path === group.path
      : (!row.project_path && row.project === group.label));
    return allRows.length && allRows.every(row => row.role === 'molecule')
      ? 'molecule_library' : 'adsorption';
  }

  // 组头行:项目名 + done/total 进度 + 细进度条 + 聚合状态;全 DONE 给「算 ΔE」
  function groupHeadHtml(key, label, rows, isProject, projectPath, projectKind, projectId) {
    const st = groupStats(rows);
    const open = State.expanded.has(key);
    const pct = st.total ? Math.round(st.done / st.total * 100) : 0;
    let agg;
    if (st.need) agg = `<span class="pill fail"><i></i>需处理 ${st.need}</span>`;
    else if (st.run) agg = VCS.pill('RUNNING');
    else if (st.queue) agg = VCS.pill('QUEUED');
    else if (st.done === st.total) agg = VCS.pill('DONE');
    else agg = `<span class="pill q"><i></i>待提交</span>`;
    const allDone = st.total > 0 && st.done === st.total;
    return `<tr class="grp-head" data-grp="${VCS.esc(key)}" ` +
      `title="点击${open ? '折叠' : '展开'}组内 ${st.total} 个作业">` +
      `<td colspan="9"><span class="caret">${open ? '▾' : '▸'}</span>` +
      `<b class="grp-name">${VCS.esc(label)}</b>` +
      (isProject ? `<span class="grp-tag">${projectKind === 'molecule_library'
        ? '分子参考库' : '吸附能项目'}</span>` : '') +
      `<span class="grp-prog">${st.done}/${st.total} 完成</span>` +
      `<span class="grp-bar"><i style="width:${pct}%"></i></span>` +
      agg +
      (isProject && projectKind !== 'molecule_library' && allDone
        ? ` <button class="btn grp-de" data-proj="${VCS.esc(label)}" ` +
          `data-proj-path="${VCS.esc(projectPath || '')}" ` +
          `data-project-id="${VCS.esc(projectId || '')}" ` +
          'title="切到吸附能项目页并选中该项目">算 ΔE</button>'
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
      card.innerHTML = '<div class="empty"><p>还没有纳管的作业 — 去生成页产出四件套,' +
        '或从集群队列认领已有作业</p></div>';
      return;
    }
    // 丢弃已不在台账里的选中项
    const present = new Set(State.rows.map(r => r.dir));
    State.selected.forEach(d => { if (!present.has(d)) State.selected.delete(d); });
    if (hadSelection && !State.selected.size) publishJobContextCleared();

    const rows0 = visibleRows();
    if (!rows0.length) {
      card.innerHTML = '<div class="empty"><p>当前筛选无匹配作业 — 调整上方筛选条件</p></div>';
      return;
    }

    // 按吸附能项目分桶(project=null → 单独作业桶);保持筛选后次序
    const groups = new Map();
    const single = [];
    rows0.forEach(r => {
      if (r.project) {
        const groupKey = r.project_path ? 'path:' + r.project_path : 'name:' + r.project;
        if (!groups.has(groupKey)) {
          groups.set(groupKey, {
            label: r.project,
            path: r.project_path || '',
            projectId: String(r.project_id || '').trim(),
            rows: [],
          });
        }
        if (!groups.get(groupKey).projectId && r.project_id) {
          groups.get(groupKey).projectId = String(r.project_id).trim();
        }
        groups.get(groupKey).rows.push(r);
      } else {
        single.push(r);
      }
    });

    let h = '<table><thead><tr>' +
      '<th class="chk"></th><th>作业</th><th>状态</th><th class="mono">作业号</th>' +
      '<th class="num">步</th><th class="num">|F|max</th><th class="num">E0 (eV)</th>' +
      '<th>诊断</th><th class="mono">更新</th></tr></thead><tbody>';
    if (!groups.size) {
      // 没有任何项目组 → 保持旧平铺观感,不加组头
      rows0.forEach(r => { h += rowHtml(r, null, false); });
    } else {
      groups.forEach((group, groupKey) => {
        const rows = group.rows;
        const key = 'p:' + groupKey;
        const open = State.expanded.has(key);
        const projectKind = projectKindForGroup(group);
        h += groupHeadHtml(
          key, group.label, rows, true, group.path, projectKind, group.projectId);
        rows.forEach(r => { h += rowHtml(r, key, !open); });
      });
      if (single.length) {
        const key = 's:_single';
        const open = State.expanded.has(key);
        h += groupHeadHtml(key, '单独作业', single, false, '', '', '');
        single.forEach(r => { h += rowHtml(r, key, !open); });
      }
    }
    h += '</tbody></table>';
    card.innerHTML = h;
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
    sel.innerHTML = '<option value="">全部集群</option>' +
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
  function toggleGroup(key) {
    if (State.expanded.has(key)) State.expanded.delete(key);
    else State.expanded.add(key);
    renderTable();
  }

  function projectRecordId(project) {
    return String(project && (project.project_id || project.id) || '').trim();
  }

  function resolveProjectTarget(name, path, explicitProjectId) {
    const wantedName = String(name || '');
    const wantedPath = String(path || '');
    let wantedId = String(explicitProjectId || '').trim();
    const matchingRows = State.rows.filter(row => wantedPath
      ? String(row.project_path || '') === wantedPath
      : (wantedName && String(row.project || '') === wantedName));
    if (!wantedId) {
      const stableIds = Array.from(new Set(matchingRows
        .map(row => String(row.project_id || '').trim()).filter(Boolean)));
      if (stableIds.length === 1) wantedId = stableIds[0];
    }
    if (!wantedId && !wantedPath && !wantedName) wantedId = currentWorkspaceProjectId();

    const workspaceProjects = VCS.workspace && Array.isArray(VCS.workspace.projects)
      ? VCS.workspace.projects : [];
    const legacyProjects = window.Project && typeof window.Project.list === 'function'
      ? window.Project.list() : [];
    const projects = workspaceProjects.concat(legacyProjects || []);
    let hit = wantedId
      ? projects.find(project => projectRecordId(project) === wantedId) || null : null;
    if (!hit && wantedPath) {
      hit = projects.find(project => String(project.path || '') === wantedPath) || null;
    }
    if (!hit && wantedName) {
      const named = projects.filter(project => String(project.name || '') === wantedName);
      if (named.length === 1) hit = named[0];
    }
    return {
      id: wantedId || projectRecordId(hit),
      path: String(hit && hit.path || wantedPath),
      name: String(hit && hit.name || wantedName),
    };
  }

  // 「算 ΔE」:先原子同步 workspace + Project 业务页，成功后再提交项目深链。
  async function gotoProject(name, path, projectId) {
    const workspace = VCS.workspace;
    let target = resolveProjectTarget(name, path, projectId);
    const knownProjects = workspace && Array.isArray(workspace.projects)
      ? workspace.projects : [];
    if (workspace && typeof workspace.refresh === 'function' &&
        (!target.id || !knownProjects.some(project => project.id === target.id))) {
      await workspace.refresh();
      target = resolveProjectTarget(name, path, projectId);
    }
    if (!target.id || !target.path || !window.Project ||
        typeof window.Project.selectByPath !== 'function') {
      VCS.log('无法打开项目：任务缺少可验证的 project_id 或项目路径', 'failc');
      VCS.toast('无法确认任务所属项目', 'fail');
      return false;
    }

    if (workspace && typeof workspace.requestProjectSwitch === 'function' &&
        typeof workspace.navigateRoute === 'function') {
      const switched = await workspace.requestProjectSwitch(target.id, async hit => {
        const canonicalPath = String(hit && hit.path || target.path);
        if (!canonicalPath) return false;
        return window.Project.selectByPath(canonicalPath);
      });
      if (!switched) return false;
      const out = await workspace.navigateRoute('project-overview', {
        projectId: target.id,
        source: 'jobs-project',
      });
      return !!(out && out.ok);
    }

    // 旧壳层兼容：仍保证业务项目先成功选中，再切换可见页面。
    const selected = await window.Project.selectByPath(target.path);
    if (!selected) return false;
    const out = await VCS.navigate('project', { source: 'jobs-project' });
    return !!(out && out.ok);
  }

  // 「导出报告」:报告在项目页生成 —— 跳项目页 + toast + 选中选中作业所属项目
  async function doReport() {
    const dirs = selectedDirs();
    let projName = '';
    let projPath = '';
    let projectId = '';
    if (dirs.length) {
      const row = State.rows.find(r => r.dir === dirs[0]);
      if (row && row.project) {
        projName = row.project;
        projPath = row.project_path || '';
        projectId = String(row.project_id || '').trim();
      }
    }
    const opened = await gotoProject(projName, projPath, projectId);
    if (!opened) return;
    VCS.toast('报告在项目页生成');
    VCS.log('报告在「吸附能项目」页生成' + (projName ? '(已为你选中项目「' + projName + '」)' : ''));
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
    el.innerHTML = `${n} 作业 · <b>${run}</b> RUN · <b>${queue}</b> QUEUE · ` +
      `<b>${done}</b> DONE · <b>${need}</b> 需处理`;
  }

  function renderStale() {
    const el = $('#jobs-stale');
    if (!el) return;
    el.textContent = State.stale.length
      ? `已隐藏 ${State.stale.length} 个失效条目(目录或 job.yaml 已不存在)——点「清理失效条目」一键移出台账`
      : '';
  }

  // ── 选中:单击单选、ctrl/cmd 多选;双击打开目录 ───────────────────────────
  function bindTable() {
    const card = $('#jobs-card');
    if (!card) return;
    card.addEventListener('click', e => {
      // 组头行:点「算 ΔE」跳项目页,其余区域折叠/展开组
      const gh = e.target.closest('tr.grp-head');
      if (gh) {
        const de = e.target.closest('.grp-de');
        if (de) {
          e.stopPropagation();
          gotoProject(
            de.dataset.proj, de.dataset.projPath || '', de.dataset.projectId || '');
          return;
        }
        toggleGroup(gh.dataset.grp);
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
      if (r && r.error) VCS.log('打开目录失败:' + r.error, 'failc');
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
    if (!acq.ok) { VCS.log('已取消(未输入密码)'); return null; }
    let password = acq.password;
    let trust = false;
    let res = await invoke(password, trust);
    for (;;) {
      if (!res) return res;
      if (res.cancelled) return null;
      if (res.needPassword) { password = res.password; res = await invoke(password, trust); continue; }
      if (res.needs_trust) {
        const pin = await VCS.confirmHostKey(res);
        if (!pin) { VCS.log('已取消或阻止未知主机连接', 'failc'); return null; }
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
        VCS.log(base(dir) + ':' + msg, ok ? 'okc' : 'failc');
      } else {
        const [dir, msg] = row;
        VCS.log(base(dir) + ':' + msg);
      }
    });
  }

  // ── 派生计算展开区(DONE 作业):频率(ZPE)/ 电子结构静态多选 ────────────────
  function fmtChange(c) {
    if (typeof c === 'string') return c;             // estatic 改动为字符串
    const key = c.key, act = c.action;
    if (act === 'add') return '新增 ' + key + ' = ' + c.new + (c.reason ? '(' + c.reason + ')' : '');
    if (act === 'strip') return '剥离 ' + key + '(原 ' + c.old + ')' + (c.reason ? ':' + c.reason : '');
    return key + ':' + c.old + ' → ' + c.new + (c.reason ? '(' + c.reason + ')' : '');
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
      '<span class="derive-lbl">派生计算:</span>' +
      '<button class="btn quiet" data-dfreq>频率 (ZPE)</button>' +
      '<span class="derive-sep">电子结构静态:</span>' +
      '<label><input type="checkbox" data-k="pdos" checked> PDOS</label>' +
      '<label><input type="checkbox" data-k="bader"> Bader</label>' +
      '<label><input type="checkbox" data-k="chgdiff"> 差分电荷</label>' +
      '<button class="btn quiet" data-dstatic>派生静态</button>' +
      '<span class="derive-sep">AIMD 热稳定性:</span>' +
      '<label>系综 <select class="ipt" data-aimd-ens style="width:auto;display:inline-block">' +
      '<option value="nvt">NVT</option><option value="nve">NVE</option></select></label>' +
      '<label>温度 <input class="ipt" data-aimd-temp value="300" style="width:56px"> K</label>' +
      '<label>步数 <input class="ipt" data-aimd-steps value="10000" style="width:72px"></label>' +
      '<button class="btn quiet" data-daimd>派生 AIMD</button></div></td>';
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
    VCS.log('派生 AIMD 作业(' + ens.toUpperCase() + ' ' + temp + 'K ' + steps + '步):' + name + ' …');
    const r = await VCS.call('derive_aimd', dir, ens, temp, null, steps, 1.0, null);
    if (!r || r.ok === false || r.error) {
      VCS.log('派生 AIMD 失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    VCS.log('已派生 AIMD 作业:' + r.job_dir, 'okc');
    (r.changes || []).forEach(c => VCS.log('  · ' + fmtChange(c)));
    (r.warnings || []).forEach(w => VCS.log('  ⚠ ' + w, 'warnc'));
    VCS.log('AIMD 作业已入台账,去列表提交', 'okc');
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
      '<span class="derive-lbl">本机运行:</span>' +
      '<label>命令模板 <input class="ipt" data-lr-cmd placeholder="如 g16 或 g16 {input} {output}" style="width:280px"></label>' +
      '<button class="btn quiet" data-lr-start>启动</button>' +
      '<button class="btn quiet" data-lr-stop>停止</button>' +
      '<span class="lr-state sub" data-lr-state>未运行</span>' +
      '<pre class="mono lr-log" data-lr-log style="margin:6px 0 0;max-height:16vh"></pre></div></td>';
    tr.parentNode.insertBefore(row, tr.nextSibling);
    row.querySelector('[data-lr-start]').addEventListener('click', () => localStart(dir, name, row));
    row.querySelector('[data-lr-stop]').addEventListener('click', () => localStop(dir, row));
    localPoll(dir, row);   // 立即拉一次状态
  }
  async function localStart(dir, name, row) {
    const tmpl = (row.querySelector('[data-lr-cmd]').value || '').trim();
    if (!tmpl) { VCS.toast('请填入本机命令模板(如 g16)', 'fail'); return; }
    VCS.log('本机运行启动:' + name + '(' + tmpl + ')…');
    const r = await VCS.call('local_run_start', dir, tmpl);
    if (!r || r.ok === false || r.error) {
      VCS.log('本机运行启动失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    VCS.log('本机运行已启动:pid ' + r.pid + '(' + (r.cmd || []).join(' ') + ')', 'okc');
    startLocalPoll(dir, row);
  }
  async function localStop(dir, row) {
    const r = await VCS.call('local_run_cancel', dir);
    if (r && r.ok) { VCS.log('已停止本机作业:' + base(dir), 'okc'); localPoll(dir, row); }
    else VCS.log('停止失败:' + ((r && r.error) || '未知'), 'failc');
  }
  async function localPoll(dir, row) {
    const r = await VCS.call('local_run_status', dir);
    if (!r || !r.ok) return;
    const st = row.querySelector('[data-lr-state]');
    if (st) st.textContent = '状态:' + (r.state || '?') +
      (r.exit_code != null ? '(退出码 ' + r.exit_code + ')' : '');
    const log = row.querySelector('[data-lr-log]');
    if (log) log.textContent = r.log_tail || '';
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
    VCS.log('派生频率作业(ZPE):' + name + ' …');
    const r = await VCS.call('derive_freq', dir);
    if (!r || r.ok === false || r.error) {
      VCS.log('派生频率失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    VCS.log('已派生频率作业:' + r.job_dir, 'okc');
    (r.changes || []).forEach(c => VCS.log('  · ' + fmtChange(c)));
    (r.warnings || []).forEach(w => VCS.log('  ⚠ ' + w, 'warnc'));
    VCS.log('频率作业已入台账,去列表提交', 'okc');
    await reload();
  }

  async function doDeriveEstatic(dir, name, kinds) {
    if (!kinds.length) { VCS.toast('请至少勾选一种静态类型', 'fail'); return; }
    VCS.log('派生静态作业(' + kinds.join('、') + '):' + name + ' …');
    const r = await VCS.call('derive_estatic', dir, kinds);
    if (!r || r.ok === false || r.error) {
      VCS.log('派生静态失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    (r.jobs || []).forEach(j => {
      VCS.log('已派生 ' + j.kind + ' 静态作业:' + j.job_dir, 'okc');
      (j.changes || []).forEach(c => VCS.log('  · ' + fmtChange(c)));
      (j.warnings || []).forEach(w => VCS.log('  ⚠ ' + w, 'warnc'));
    });
    (r.skipped || []).forEach(s => VCS.log('跳过 ' + s.kind + ':' + s.reason, 'warnc'));
    VCS.log('静态作业已入台账,去列表提交', 'okc');
    await reload();
  }

  // ── 自旋对比:对选中的自旋家族判基态 + 磁矩审计 ────────────────────────────
  async function doSpinCompare() {
    const dirs = selectedDirs();
    if (dirs.length < 2) {
      VCS.log('自旋对比:请选中同一家族的多个自旋变体(_spin_nm/_ls/_hs)', 'failc'); return;
    }
    VCS.log('自旋对比(' + dirs.length + ' 个变体)…');
    const r = await VCS.call('spin_family_compare', dirs);
    if (!r || r.ok === false || r.error) {
      VCS.log('自旋对比失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    const g = r.ground || {};
    if (g.pending && g.pending.length) {
      VCS.log('尚未全部 DONE,待完成:' + g.pending.join('、'), 'warnc');
    } else if (g.winner) {
      VCS.log('自旋基态:' + g.winner + '(相对能量 ' +
        Object.entries(g.de_meV || {}).map(kv => kv[0] + '=' + kv[1] + ' meV').join(', ') + ')', 'okc');
      if (g.warning) VCS.log('⚠ ' + g.warning, 'warnc');
    }
    (r.audits || []).forEach(a => {
      if (!a.audited) VCS.log('磁矩审计 ' + a.name + ':' + (a.warning || '无法审计'), 'warnc');
      else if (a.warning) VCS.log('磁矩审计 ' + a.name + ':⚠ ' + a.warning, 'warnc');
      else VCS.log('磁矩审计 ' + a.name + ':末态 ' + a.final_magnetization + ' μB,正常', 'okc');
    });
    if (r.report_file) VCS.log('多自旋态可追溯报告:' + r.report_file, 'okc');
    if (r.report_error) VCS.log('多自旋报告生成失败:' + r.report_error, 'warnc');
  }

  // ── 上传并提交 ─────────────────────────────────────────────────────────────
  async function doSubmit() {
    const name = requireProfile();
    if (!name) return;
    const dirs = actionDirs(name, '提交', 'new');
    if (dirs === null) return;
    if (!dirs.length) { VCS.log('请先在列表中选中要提交的作业(可多选)', 'failc'); return; }
    const prof = State.profiles[name];
    const ok = await VCS.confirm(
      `将上传并提交 ${dirs.length} 个作业到「${name}」\n` +
      `远程根目录:${prof.remote_root || '(未设置)'}\n脚本模式:${prof.script_mode}\n\n继续?`);
    if (!ok) return;
    VCS.log(`连接并提交 ${dirs.length} 个作业…`);
    const res = await remote(name, (pw, trust) => VCS.call('submit_jobs', dirs, name, pw, trust));
    if (!res) return;
    if (res.error) { VCS.log('提交异常:' + res.error, 'failc'); return; }
    logResults(res.results, true);
    await reload();
  }

  // ── 查询状态(手动 / 自动共用) ────────────────────────────────────────────
  async function runStatus(auto) {
    const name = auto ? currentProfile() : requireProfile();
    if (!name || !State.profiles[name]) return;   // auto 无集群静默跳过
    if (State.refreshing) {
      if (!auto) VCS.log('上一轮查询仍在进行,请稍候');
      return;
    }
    if (auto) {
      const prof = State.profiles[name];
      if (prof.auth === 'password') {
        const hp = await VCS.call('has_saved_password', name);
        if (!(hp && hp.saved)) {
          VCS.log('自动刷新需先手动查询一次并保存密码,本轮跳过', 'failc');
          return;
        }
      }
    }
    State.refreshing = true;
    try {
      if (!auto) VCS.log('查询作业状态…');
      const res = await remote(name, (pw, trust) => VCS.call('refresh_status', name, pw, trust));
      if (!res) return;
      if (res.error) {
        VCS.log((auto ? '自动刷新' : '查询') + '异常:' + res.error, 'failc');
        // 自动刷新失败在别页也要可见(此前只写日志,用户看不到)
        if (auto) VCS.toast('自动刷新失败:' + res.error, 'fail');
        return;
      }
      const results = res.results || [];
      if (!results.length) {
        if (!auto) VCS.log(`「${name}」没有待查询的作业(SUBMITTED/QUEUED/RUNNING)`);
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
      if (!auto) VCS.log('上一轮查询仍在进行,请稍候');
      return;
    }
    State.refreshing = true;
    try {
      if (!auto) VCS.log('并行查询所有服务器上的活跃作业…');
      const res = await VCS.call('refresh_all_status');
      if (!res || res.error) {
        VCS.log('全部服务器查询失败:' + ((res && res.error) || '未知错误'), 'failc');
        return;
      }
      let count = 0;
      (res.profiles || []).forEach(server => {
        if (server.error) {
          const next = server.error === 'NEED_PASSWORD'
            ? '请在目标集群下手动查询一次并保存密码'
            : server.needs_trust ? '请先选择该集群手动查询并核对 SHA256 指纹' : server.error;
          VCS.log(`[${server.name}] 查询失败:${next}`, 'failc');
          return;
        }
        (server.results || []).forEach(row => {
          count++;
          VCS.log(`[${server.name}] ${row[0]}: ${row[1]}`);
        });
      });
      if (!count && !auto && !(res.errors || []).length) {
        VCS.log('所有服务器都没有待查询的活跃作业');
      }
      if ((res.errors || []).length && auto) {
        VCS.toast(`多服务器监控：${res.errors.length} 台需要处理，其它服务器已正常刷新`, 'fail');
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
        ['按任务自动（推荐）：能带 / DOS / Bader / ELF / 功函数 / AIMD 各取自己的关键文件', null],
        ['仅轻量：CONTCAR + OSZICAR + OUTCAR', ['CONTCAR', 'OSZICAR', 'OUTCAR']],
        ['电子结构全家桶：轻量 + vasprun.xml + DOSCAR + EIGENVAL + CHGCAR + AECCAR0/2 + ELFCAR + LOCPOT',
          ['CONTCAR', 'OSZICAR', 'OUTCAR', 'vasprun.xml', 'DOSCAR', 'EIGENVAL',
            'CHGCAR', 'AECCAR0', 'AECCAR2', 'ACF.dat', 'ELFCAR', 'LOCPOT', 'XDATCAR']],
      ];
      let done = false;
      const finish = v => { if (!done) { done = true; resolve(v); } };
      const body =
        presets.map((p, i) => `<label style="display:block;margin-bottom:8px"><input type="radio" name="fp" value="${i}"${i === 0 ? ' checked' : ''}> ${VCS.esc(p[0])}</label>`).join('') +
        `<label style="display:block;margin-bottom:6px"><input type="radio" name="fp" value="99"> 自定义(逗号分隔文件名):</label>` +
        `<input id="fp-custom" class="ipt" value="CONTCAR, OUTCAR, vasprun.xml">`;
      const m = VCS.modal({
        title: `拉回结果 — ${n} 个作业`,
        bodyHTML: body,
        actions: [
          { label: '取消', quiet: true, onClick: mm => { mm.close(); finish(null); } },
          { label: '开始拉回', primary: true, onClick: mm => {
              const c = mm.el.querySelector('input[name=fp]:checked').value;
              let files;
              if (c === '99') {
                files = mm.el.querySelector('#fp-custom').value
                  .split(',').map(s => s.trim()).filter(Boolean);
                if (!files.length) { VCS.toast('自定义文件列表为空', 'fail'); return; }
              } else {
                files = presets[+c][1];
              }
              mm.close(); finish({ files, label: c === '99' ? '自定义结果包' : presets[+c][0] });
            } },
        ],
      });
      m.onDismiss = () => finish(null);
    });
  }

  async function doFetch() {
    const name = requireProfile();
    if (!name) return;
    const dirs = actionDirs(name, '拉回结果', 'bound');
    if (dirs === null) return;
    if (!dirs.length) { VCS.log('请先选中要拉回结果的作业(通常是 DONE/未收敛 的)', 'failc'); return; }
    const choice = await askFetchFiles(dirs.length);
    if (!choice) return;
    const files = choice.files;
    VCS.log(`拉回 ${dirs.length} 个作业：${files ? files.join('、') : choice.label}…`);
    const res = await remote(name, (pw, trust) =>
      VCS.call('fetch_jobs', dirs, name, pw, trust, files));
    if (!res) return;
    if (res.error) { VCS.log('拉回异常:' + res.error, 'failc'); return; }
    logResults(res.results, true);
    await reload();
  }

  // ── 续算(有界恢复) ───────────────────────────────────────────────────────
  async function doContinue() {
    const name = requireProfile();
    if (!name) return;
    const dirs = actionDirs(name, '续算', 'bound');
    if (dirs === null) return;
    if (!dirs.length) { VCS.log('请先选中要续算的作业(仅未收敛/墙钟/ZBRENT 等可续算)', 'failc'); return; }
    const ok = await VCS.confirm(
      `将对选中的 ${dirs.length} 个作业从 CONTCAR 续算并重投到「${name}」\n` +
      `INCAR 冻结;每作业上限 3 轮。不可续算的会被后端跳过。\n\n继续?`);
    if (!ok) return;
    VCS.log(`连接并续算 ${dirs.length} 个作业…`);
    const res = await remote(name, (pw, trust) => VCS.call('continue_jobs', dirs, name, pw, trust));
    if (!res) return;
    if (res.error) { VCS.log('续算异常:' + res.error, 'failc'); return; }
    logResults(res.results, true);
    await reload();
  }

  // ── 集群队列 + 认领 ────────────────────────────────────────────────────────
  async function doQueue() {
    const name = requireProfile();
    if (!name) return;
    VCS.log(`查询「${name}」上的全部队列作业…`);
    const res = await remote(name, (pw, trust) => VCS.call('queue_detail', name, pw, trust));
    if (!res) return;
    if (res.error) { VCS.log('队列查询异常:' + res.error, 'failc'); return; }
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
    if (!jobs.length) VCS.log(`「${name}」队列为空(该用户当前没有在队/在跑作业)`);
    // 认领本地根目录(未配置 → 后端给默认 %USERPROFILE%\vcstudio_jobs)
    const rr = await VCS.call('adopt_root_get');
    let root = (rr && rr.root) || '';
    const unmanaged = jobs.filter(j => !known.has(String(j.job_id)));

    // 顶部头行:一键认领全部未纳入(N,为 0 时隐藏)+ 当前认领根目录 + 修改链接
    const header =
      '<div style="display:flex;justify-content:space-between;align-items:center;' +
      'gap:10px;margin-bottom:8px;flex-wrap:wrap">' +
      '<div>' + (unmanaged.length
        ? `<button class="btn primary" id="adopt-all-btn">一键认领全部未纳入(${unmanaged.length})</button>`
        : '') + '</div>' +
      '<div class="sub">本地根目录:<span id="adopt-root-val" class="mono">' +
      `${VCS.esc(root)}</span> <a href="#" id="adopt-root-edit">修改</a></div></div>`;

    let h = header + '<div style="max-height:52vh;overflow:auto"><table><thead><tr>' +
      '<th class="mono">作业号</th><th>状态</th><th>作业名</th><th>远程目录</th><th>纳管</th>' +
      '</tr></thead><tbody>';
    jobs.forEach((j, i) => {
      const managed = known.has(String(j.job_id));
      const st = j.state === 'RUNNING' ? '运行中' : '排队中';
      h += `<tr data-i="${i}">` +
        `<td class="mono">${VCS.esc(j.job_id)}</td>` +
        `<td>${VCS.esc(st)}</td>` +
        `<td>${VCS.esc(j.name || '')}</td>` +
        `<td class="sub">${VCS.esc(j.workdir || '')}</td>` +
        `<td>${managed ? '已纳管'
          : `<button class="btn quiet" data-claim="${i}">认领</button>`}</td></tr>`;
    });
    h += '</tbody></table></div>' +
      '<p class="sub" style="margin-top:8px">「认领」= 把不是本软件提交的作业(如终端手动 qsub)' +
      '纳入台账,之后可查状态/拉回/续算。「一键认领」自动补远程工作目录,本地目录建在根目录下。</p>';
    const m = VCS.modal({
      title: `集群队列 — ${name}`,
      bodyHTML: h,
      actions: [{ label: '关闭', onClick: mm => mm.close() }],
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
        `将认领 ${unmanaged.length} 个作业,本地目录建在 ${root} 下`);
      if (!ok) return;
      m.close();
      VCS.log(`一键认领 ${unmanaged.length} 个未纳入作业(自动补远程目录)…`);
      const res = await remote(name, (pw, trust) => VCS.call('adopt_all', name, pw, trust));
      if (!res) return;
      if (res.error) { VCS.log('一键认领异常:' + res.error, 'failc'); return; }
      logResults(res.results, true);
      await reload();
    });

    // 单个认领
    m.el.querySelectorAll('button[data-claim]').forEach(btn => {
      btn.addEventListener('click', () => {
        const j = jobs[+btn.dataset.claim];
        askAdopt(name, j, root, () => {
          if (btn.parentNode) btn.parentNode.textContent = '已纳管';
        });
      });
    });
  }

  // 修改认领根目录:prompt 式 modal
  function editAdoptRoot(current, onSaved) {
    const body =
      '<div class="sub" style="margin-bottom:4px">认领作业时,本地目录建在此根目录下</div>' +
      `<input id="ar-input" class="ipt" value="${VCS.esc(current || '')}" placeholder="例如 E:\\runs">`;
    VCS.modal({
      title: '设置认领根目录',
      bodyHTML: body,
      actions: [
        { label: '取消', quiet: true, onClick: mm => mm.close() },
        { label: '保存', primary: true, onClick: async mm => {
            const v = mm.el.querySelector('#ar-input').value.trim();
            if (!v) { VCS.toast('根目录不能为空', 'fail'); return; }
            const r = await VCS.call('adopt_root_set', v);
            if (r && r.error) { VCS.log('保存根目录失败:' + r.error, 'failc'); return; }
            mm.close();
            VCS.log('认领根目录已更新:' + v, 'okc');
            if (onSaved) onSaved(v);
          } },
      ],
    });
  }

  function askAdopt(name, j, root, onDone) {
    const localDefault = joinLocal(root, j.name || j.job_id);
    const hasWd = !!(j.workdir && j.workdir.trim());
    const body =
      `<div style="margin-bottom:10px"><div class="sub" style="margin-bottom:4px">远程目录(绝对路径,以 / 开头)</div>` +
      `<input id="ad-remote" class="ipt" value="${VCS.esc(j.workdir || '')}" ` +
      `placeholder="${hasWd ? '/home/<用户名>/runs' : '查询中…'}"></div>` +
      `<div><div class="sub" style="margin-bottom:4px">本地目录(结果将拉回到这里;不存在会新建)</div>` +
      `<input id="ad-local" class="ipt" value="${VCS.esc(localDefault)}" placeholder="例如 E:\\runs\\claimed_${VCS.esc(j.job_id)}"></div>`;
    const m = VCS.modal({
      title: `认领作业 ${VCS.esc(j.job_id)}${j.name ? ' · ' + VCS.esc(j.name) : ''}`,
      bodyHTML: body,
      actions: [
        { label: '取消', quiet: true, onClick: mm => mm.close() },
        { label: '认领', primary: true, onClick: async mm => {
            const remoteDir = mm.el.querySelector('#ad-remote').value.trim();
            const local = mm.el.querySelector('#ad-local').value.trim();
            if (!local) { VCS.toast('请填写本地目录', 'fail'); return; }
            if (!remoteDir) { VCS.toast('请填写远程目录', 'fail'); return; }
            mm.close();
            const r = await VCS.call('adopt_job', local, name, String(j.job_id), remoteDir, j.name || '');
            if (r && r.error) { VCS.log('认领失败:' + r.error, 'failc'); return; }
            VCS.log(`已认领作业 ${j.job_id} → ${local}(下次「查询状态」即可追踪)`, 'okc');
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
          inp.placeholder = '/home/<用户名>/runs';
        });
    }
  }

  // ── 打开目录 / 移出台账 / 清理失效 ─────────────────────────────────────────
  async function doOpen() {
    const dirs = selectedDirs();
    if (!dirs.length) { VCS.log('请先选中一个作业再打开目录', 'failc'); return; }
    const r = await VCS.call('open_dir', dirs[0]);
    if (r && r.error) VCS.log('打开目录失败:' + r.error, 'failc');
  }

  async function doRemove() {
    const dirs = selectedDirs();
    if (!dirs.length) { VCS.log('请先选中要移出台账的作业', 'failc'); return; }
    const ok = await VCS.confirm(`把 ${dirs.length} 个作业移出台账?(不删除磁盘文件)`);
    if (!ok) return;
    const r = await VCS.call('remove_jobs', dirs);
    if (r && r.error) { VCS.log('移出失败:' + r.error, 'failc'); return; }
    VCS.log(`已移出 ${dirs.length} 个作业`, 'okc');
    await reload();
  }

  async function doClean() {
    if (!State.stale.length) { VCS.log('没有失效条目,无需清理'); return; }
    const ok = await VCS.confirm(
      `将把 ${State.stale.length} 个失效条目(目录或 job.yaml 已不存在)移出台账。\n` +
      `不删除磁盘文件。继续?`);
    if (!ok) return;
    const r = await VCS.call('clean_stale');
    if (r && r.error) { VCS.log('清理失败:' + r.error, 'failc'); return; }
    VCS.log(`已清理 ${(r && r.removed) || 0} 个失效条目`, 'okc');
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
      if (!silent) VCS.log(`自动刷新开启(每 ${mins} 分钟)`);
    } else if (!silent) {
      VCS.log('自动刷新已关闭');
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
    const dirs = actionDirs(name, '批量取消', 'bound');
    if (dirs === null) return;
    if (!dirs.length) { VCS.log('批量取消:请先勾选要取消的作业', 'failc'); return; }
    if (!await VCS.confirm('确认取消勾选的 ' + dirs.length + ' 个作业?(qdel/scancel + 台账标记 FAILED/用户取消)')) return;
    const res = await remote(name, (pw, trust) => VCS.call('jobs_cancel_batch', dirs, name, pw, trust));
    if (!res) return;
    if (res.error) { VCS.log('批量取消失败:' + res.error, 'failc'); return; }
    (res.cancelled || []).forEach(j => VCS.log('已取消作业号 ' + j, 'okc'));
    (res.failed || []).forEach(f => VCS.log('取消失败 ' + f.job_id + ':' + f.reason, 'failc'));
    await reload();
  }

  // ── 快速批量提交:父目录扫描 → 四件套补齐 → 建作业 → 自动勾选 ──
  const QS = { files: [], scan: null, scanSeq: 0, scanning: false };
  function qsSharedIncar() { return ($('#qs-incar') && $('#qs-incar').value.trim()) || ''; }
  function qsBuildable() {
    return (((QS.scan || {}).items) || []).filter(i => i && i.can_build).length;
  }
  function qsUpdateBuildButton() {
    const btn = $('#qs-build');
    if (!btn) return;
    const n = qsBuildable();
    btn.disabled = QS.scanning || n === 0;
    btn.textContent = QS.scanning ? '正在扫描…' :
      (n ? `生成 / 导入 ${n} 个作业` : '请先选择并预检');
  }
  function renderQsFiles() {
    const box = $('#qs-filelist');
    const cnt = $('#qs-count');
    if (cnt) cnt.textContent = QS.files.length + ' 个入口';
    if (!box) return;
    box.innerHTML = QS.files.map((f, i) =>
      `<span class="qs-file" data-i="${i}">${VCS.esc(f)} <b data-rm="${i}">×</b></span>`).join('') ||
      '<span class="sub">尚未选择。优先选择包含多个计算目录的父文件夹</span>';
  }
  function qsStatus(item) {
    if (item.status === 'ready') return ['ready', '四件套完整'];
    if (item.status === 'generatable') return ['generatable', '可自动补齐'];
    return ['blocked', '需要处理'];
  }
  function renderQsPreview(r) {
    const box = $('#qs-preview');
    if (!box) return;
    if (!QS.files.length) { box.hidden = true; box.innerHTML = ''; qsUpdateBuildButton(); return; }
    box.hidden = false;
    if (!r || r.error || r.ok === false) {
      box.innerHTML = `<div class="qs-preview-head"><strong>预检失败</strong>` +
        `<span class="qs-stat bad">${VCS.esc((r && r.error) || '无法读取输入')}</span></div>`;
      qsUpdateBuildButton();
      return;
    }
    const s = r.summary || {};
    const items = r.items || [];
    let h = '<div class="qs-preview-head"><strong>预检结果：发现 ' +
      VCS.esc(s.total || 0) + ' 个候选作业</strong>' +
      `<span class="qs-stat ok">完整 ${VCS.esc(s.ready || 0)}</span>` +
      `<span class="qs-stat gen">可生成 ${VCS.esc(s.generatable || 0)}</span>` +
      `<span class="qs-stat bad">需处理 ${VCS.esc(s.blocked || 0)}</span>` +
      ((s.duplicates || 0) ? `<span class="qs-stat">已去重 ${VCS.esc(s.duplicates)}</span>` : '') +
      '</div><div class="qs-preview-list">';
    if (!items.length) {
      h += '<div class="empty"><p>没有发现可用输入。请选择包含 POSCAR、CONTCAR 或四件套的目录。</p></div>';
    } else {
      items.forEach(item => {
        const st = qsStatus(item);
        const missing = item.missing || [];
        h += '<div class="qs-preview-row">' +
          `<div class="qs-preview-name"><b>${VCS.esc(item.name || '未命名')}</b>` +
          `<span title="${VCS.esc(item.path || '')}">${VCS.esc(item.path || '')}</span></div>` +
          `<div><span class="qs-status ${st[0]}">${st[1]}</span>` +
          (missing.length
            ? `<span class="qs-missing${item.can_build ? '' : ' bad'}">缺：${VCS.esc(missing.join('、'))}</span>`
            : '<span class="qs-missing">文件完整</span>') + '</div>' +
          `<div class="qs-preview-msg">${VCS.esc(item.message || '')}</div></div>`;
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
      box.innerHTML = '<div class="qs-preview-head"><strong>正在递归扫描目录并检查四件套…</strong></div>';
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
    if (r && r.error) { VCS.log('选择文件失败:' + r.error, 'failc'); return; }
    if (r && r.path && QS.files.indexOf(r.path) < 0) {
      QS.files.push(r.path); renderQsFiles(); qsSuggestOut(r.path, false); await qsScan();
    }
  }
  async function qsAddVaspDir() {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log('选择目录失败:' + r.error, 'failc'); return; }
    if (r && r.path && QS.files.indexOf(r.path) < 0) {
      QS.files.push(r.path);
      renderQsFiles();
      qsSuggestOut(r.path, true);
      VCS.log('正在扫描父目录:' + r.path);
      const scan = await qsScan();
      if (scan && scan.ok) {
        const s = scan.summary || {};
        VCS.log(`已发现 ${s.total || 0} 个候选作业：完整 ${s.ready || 0}，` +
          `可自动补齐 ${s.generatable || 0}，需处理 ${s.blocked || 0}`, 'okc');
      }
    }
  }
  async function qsBuild() {
    if (!QS.files.length) { VCS.log('快速提交:请先添加 VASP 目录或输入文件', 'failc'); return; }
    if (!QS.scan || QS.scanning) await qsScan();
    if (!qsBuildable()) {
      VCS.log('没有可建作业。若只有 POSCAR/CONTCAR，请先选择共享 INCAR；并检查预检中的缺项。', 'failc');
      return;
    }
    const out = ($('#qs-out') && $('#qs-out').value.trim()) || '';
    if (!out) { VCS.log('快速提交:请选择输出根目录', 'failc'); return; }
    const shared = qsSharedIncar();
    const lib = ($('#qs-lib') && $('#qs-lib').value.trim()) || '';
    const calcType = ($('#qs-calc-type') && $('#qs-calc-type').value) || 'slab';
    VCS.log('正在生成 / 导入 ' + qsBuildable() + ' 个作业；源文件保持不变…');
    const r = await VCS.call(
      'quick_submit_build', QS.files, out, '', shared, lib, calcType);
    if (!r || r.ok === false || r.error) {
      VCS.log('快速提交失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    (r.jobs || []).forEach(j => {
      VCS.log('已建作业:' + j.name + '(' + j.engine + ')→ ' + j.dir, 'okc');
      if (j.hint) VCS.log('  提交命令模板:' + j.hint);
    });
    (r.skipped || []).forEach(s => VCS.log('跳过 ' + s.file + ':' + s.reason, 'warnc'));
    const made = r.jobs || [];
    VCS.log('已建 ' + made.length + ' 个作业并入台账', 'okc');
    VCS.toast('已准备 ' + made.length + ' 个作业');
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
      next.hidden = false;
      if ($('#qs-next-title')) $('#qs-next-title').textContent = `已准备 ${made.length} 个作业`;
      if ($('#qs-next-note')) $('#qs-next-note').textContent =
        `其中 ${selected} 个已在下方自动勾选；提交前仍会显示集群与远程目录确认。`;
      next.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  // ── 文件管理:列远端目录 + 下载选中 ──
  const FM = { entries: [], checked: new Set() };
  function fmClusters() {
    const sel = $('#fm-cluster');
    if (!sel) return;
    const names = Object.keys(State.profiles);
    sel.innerHTML = names.length
      ? names.map(n => `<option value="${VCS.esc(n)}">${VCS.esc(n)}</option>`).join('')
      : '<option value="">(未配置集群)</option>';
  }
  async function fmList() {
    const name = $('#fm-cluster') ? $('#fm-cluster').value : '';
    if (!name || !State.profiles[name]) { VCS.log('文件管理:请先选集群', 'failc'); return; }
    const path = ($('#fm-path') && $('#fm-path').value.trim()) || '.';
    VCS.log('列远端目录:' + name + ':' + path + ' …');
    const res = await remote(name, (pw, trust) => VCS.call('remote_ls', name, pw, path, trust));
    if (!res) return;
    if (res.error) { VCS.log('列目录失败:' + res.error, 'failc'); return; }
    FM.entries = res.entries || [];
    FM.checked = new Set();
    renderFmTable(res.path || path);
    VCS.log('已列出 ' + FM.entries.length + ' 项', 'okc');
  }
  function renderFmTable(path) {
    const box = $('#fm-table');
    if (!box) return;
    if (!FM.entries.length) { box.innerHTML = '<span class="sub">目录为空:' + VCS.esc(path) + '</span>'; return; }
    let h = '<table class="fm-tbl"><thead><tr><th></th><th>名称</th><th class="num">大小</th>' +
      '<th>修改时间</th></tr></thead><tbody>';
    FM.entries.forEach((e, i) => {
      const dt = e.mtime ? new Date(e.mtime * 1000).toLocaleString() : '';
      const icon = e.is_dir ? '📁 ' : '';
      h += `<tr data-fi="${i}"><td>${e.is_dir ? '' : `<input type="checkbox" class="fm-chk" data-i="${i}">`}</td>` +
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
    if (!name) { VCS.log('文件管理:请先选集群', 'failc'); return; }
    const localDir = ($('#fm-localdir') && $('#fm-localdir').value.trim()) || '';
    if (!localDir) { VCS.log('文件管理:请选择本地保存目录', 'failc'); return; }
    const picks = Array.from(FM.checked);
    if (!picks.length) { VCS.log('文件管理:请勾选要下载的文件', 'failc'); return; }
    const base0 = ($('#fm-path') && $('#fm-path').value.trim()) || '.';
    for (const i of picks) {
      const e = FM.entries[i];
      if (!e) continue;
      const rpath = base0.replace(/\/+$/, '') + '/' + e.name;
      const res = await remote(name, (pw, trust) =>
        VCS.call('remote_fetch_file', name, pw, rpath, localDir, trust));
      if (!res) return;
      if (res.error) VCS.log('下载失败 ' + e.name + ':' + res.error, 'failc');
      else VCS.log('已下载:' + res.local_path, 'okc');
    }
    VCS.toast('下载完成');
    VCS.call('open_dir', localDir);
  }

  // ── 初始化 ─────────────────────────────────────────────────────────────────
  // ── v3.3.0 实时能量曲线:本地 OSZICAR 优先,远端 SSH 轮询(对齐 starpivot 任务监控) ──
  const Live = { dir: null, name: '', cluster: '', pw: null, timer: null, chart: null, busy: false };
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
    const note = $('#jl-note'); if (note) note.textContent = '读取中…';
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
    const note = $('#jl-note');
    if (!res || res.ok === false || res.error) {
      if (note) note.textContent = '⚠ ' + ((res && res.error) || '读取失败');
      return;
    }
    const steps = res.steps || [];
    if (!steps.length) {
      if (note) note.textContent = '尚无离子步(SCF 进行中或刚启动),轮询将自动更新';
      return;
    }
    const last = steps[steps.length - 1];
    if (note) {
      note.textContent = (res.source === 'remote' ? '远端 OSZICAR' : '本地 OSZICAR') +
        ' · ' + steps.length + ' 离子步 · 末步 E0=' + last.e0 + ' eV' +
        (last.de != null ? ' · |ΔE|=' + last.de + ' eV' : '') +
        (res.state ? ' · 状态 ' + res.state : '') +
        (Live.timer ? '' : '(手动刷新模式)');
    }
    const box = document.getElementById('jl-chart');
    if (!box) return;
    if (typeof echarts === 'undefined') { box.textContent = '(echarts 未加载,无法画曲线)'; return; }
    if (!Live.chart) Live.chart = echarts.init(box);
    Live.chart.setOption({
      grid: { left: 74, right: 20, top: 24, bottom: 40 },
      xAxis: { type: 'category', name: '离子步', data: steps.map(s => s.n) },
      yAxis: { type: 'value', name: 'E0 (eV)', scale: true },
      tooltip: { trigger: 'axis' },
      series: [{ type: 'line', symbolSize: 5, data: steps.map(s => s.e0) }],
    }, true);
  }

  function wire(id, fn) { const el = $('#' + id); if (el) el.addEventListener('click', fn); }

  function init() {
    ensureTableScrollRegion();
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
      renderQsFiles(); renderQsPreview(null);
      const next = $('#qs-next'); if (next) next.hidden = true;
    });
    wire('qs-incar-btn', async () => {
      const r = await VCS.call('pick_file', 'incar');
      if (r && r.error) { VCS.log('选择 INCAR 失败:' + r.error, 'failc'); return; }
      if (r && r.path && $('#qs-incar')) {
        $('#qs-incar').value = r.path;
        VCS.log('已选择共享 INCAR，重新检查可自动补齐的结构…');
        await qsScan();
      }
    });
    wire('qs-lib-btn', async () => {
      const r = await VCS.call('pick_dir');
      if (r && r.error) { VCS.log('选择赝势库失败:' + r.error, 'failc'); return; }
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
      if (removed) VCS.log(`切换服务器后已取消 ${removed} 个其它服务器作业的勾选`, 'warnc');
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
    const projectId = String(detail.id || detail.project_id || detail.project_uuid || '').trim();
    const selectedRows = Array.from(State.selected)
      .map(dir => State.rows.find(row => row.dir === dir)).filter(Boolean);
    const belongsToProject = projectId && selectedRows.length && selectedRows.every(row =>
      String(row.project_id || row.project_uuid || '').trim() === projectId);
    if (!belongsToProject) {
      State.selected.clear();
      renderTable();
      publishJobContextCleared();
    }
  }
  document.addEventListener('vcs:workspace-project', invalidateJobSelectionForProject);
  // Project 业务页会在壳层事件之前发布稳定项目上下文；这是旧调用路径的兜底。
  document.addEventListener('vcs:project-context', invalidateJobSelectionForProject);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // 结果导入可能带来只有完整四件套、尚未运行的 CREATED 成员。
  // 进入任务页时按 project_path 精确展开并勾选，用户仍需亲自确认服务器与远程目录。
  async function selectCreatedProject(projectPath) {
    const selectionGeneration = ++State.selectionGeneration;
    await reload();
    if (selectionGeneration !== State.selectionGeneration) return 0;
    const wanted = String(projectPath || '');
    const rows = State.rows.filter(row => row.state === 'CREATED' && !row.cluster &&
      (!wanted || String(row.project_path || '') === wanted));
    rows.forEach(row => State.selected.add(row.dir));
    if (wanted) State.expanded.add('path:' + wanted);
    ['jf-cluster', 'jf-status'].forEach(id => {
      const filter = $('#' + id);
      if (filter && filter.value) filter.value = '';
    });
    renderTable();
    if (rows.length) VCS.toast(`已勾选 ${rows.length} 个待提交作业；请选择服务器后提交`);
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
    if (row.project) {
      const groupKey = row.project_path ? 'path:' + row.project_path : 'name:' + row.project;
      State.expanded.add('p:' + groupKey);
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

  // 供集群页保存与项目导入流程调用。
  window.Jobs = { reload, selectCreatedProject, selectById, clearSelection };
})();
