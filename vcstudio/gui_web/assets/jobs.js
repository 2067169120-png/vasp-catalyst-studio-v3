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
  };

  // 目录 → 末段名(本地日志用,兼容 \ 与 /)
  function base(p) { return String(p).replace(/[\\/]+$/, '').split(/[\\/]/).pop(); }
  function currentProfile() { const s = $('#jobs-profile'); return s ? s.value : ''; }
  function selectedDirs() { return Array.from(State.selected); }

  function requireProfile() {
    const name = currentProfile();
    if (!name || !State.profiles[name]) {
      VCS.log('请先在「集群」页配置并保存一个集群,再在上方选择目标集群', 'failc');
      return null;
    }
    return name;
  }

  // ── 台账取数 + 渲染 ────────────────────────────────────────────────────────
  async function loadProfiles() {
    const r = await VCS.call('list_profiles');
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

  async function reload() {
    await loadProfiles();
    const r = await VCS.call('list_jobs');
    State.rows = (r && r.jobs) || [];
    State.stale = (r && r.stale) || [];
    if (r && r.error) VCS.log('读取台账失败:' + r.error, 'failc');
    refreshClusterFilter();
    if (typeof fmClusters === 'function') fmClusters();
    renderTable();
    renderStats();
    renderStale();
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
    return `<tr data-dir="${VCS.esc(r.dir)}" data-name="${VCS.esc(r.name)}"` +
      (grpKey ? ` data-grp="${VCS.esc(grpKey)}"` : '') +
      (hidden ? ' hidden' : '') + `${sel}>` +
      `<td class="chk"><input type="checkbox" class="jrow-chk"${checked ? ' checked' : ''}></td>` +
      `<td>${VCS.elementBadge(r.name)}<span class="name">${VCS.esc(r.name)}</span>` +
      (role ? ` <span class="role-tag">${VCS.esc(role)}</span>` : '') +
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
  function groupHeadHtml(key, label, rows, isProject, projectPath, projectKind) {
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
          'title="切到吸附能项目页并选中该项目">算 ΔE</button>'
        : '') +
      `</td></tr>`;
  }

  function renderTable() {
    const card = $('#jobs-card');
    if (!card) return;
    if (!State.rows.length) {
      State.selected.clear();
      card.innerHTML = '<div class="empty"><p>还没有纳管的作业 — 去生成页产出四件套,' +
        '或从集群队列认领已有作业</p></div>';
      return;
    }
    // 丢弃已不在台账里的选中项
    const present = new Set(State.rows.map(r => r.dir));
    State.selected.forEach(d => { if (!present.has(d)) State.selected.delete(d); });

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
          groups.set(groupKey, { label: r.project, path: r.project_path || '', rows: [] });
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
        h += groupHeadHtml(key, group.label, rows, true, group.path, projectKind);
        rows.forEach(r => { h += rowHtml(r, key, !open); });
      });
      if (single.length) {
        const key = 's:_single';
        const open = State.expanded.has(key);
        h += groupHeadHtml(key, '单独作业', single, false, '', '');
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
  function refreshClusterFilter() {
    const sel = $('#jf-cluster');
    if (!sel) return;
    const cur = sel.value;
    const clusters = Array.from(new Set(State.rows.map(r => r.cluster).filter(Boolean)));
    sel.innerHTML = '<option value="">全部集群</option>' +
      clusters.map(c => `<option value="${VCS.esc(c)}">${VCS.esc(c)}</option>`).join('');
    if (clusters.indexOf(cur) >= 0) sel.value = cur;
  }

  // 组头折叠开关:记忆到 State.expanded(重载/自动刷新后保持)
  function toggleGroup(key) {
    if (State.expanded.has(key)) State.expanded.delete(key);
    else State.expanded.add(key);
    renderTable();
  }

  // 「算 ΔE」:切到吸附能项目页并尽量选中该项目
  function gotoProject(name, path) {
    const a = document.querySelector('nav a[data-page=project]');
    if (a) a.click();
    if (path && window.Project && typeof window.Project.selectByPath === 'function') {
      window.Project.selectByPath(path);
    } else if (window.Project && typeof window.Project.selectByName === 'function') {
      window.Project.selectByName(name);
    }
  }

  // 「导出报告」:报告在项目页生成 —— 跳项目页 + toast + 选中选中作业所属项目
  function doReport() {
    const dirs = selectedDirs();
    let projName = '';
    let projPath = '';
    if (dirs.length) {
      const row = State.rows.find(r => r.dir === dirs[0]);
      if (row && row.project) {
        projName = row.project;
        projPath = row.project_path || '';
      }
    }
    gotoProject(projName, projPath);
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
          gotoProject(de.dataset.proj, de.dataset.projPath || '');
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
        if (State.selected.has(dir)) State.selected.delete(dir);
        else State.selected.add(dir);
        tr.classList.toggle('sel', State.selected.has(dir));
        return;
      }
      if (e.ctrlKey || e.metaKey) {
        if (State.selected.has(dir)) State.selected.delete(dir);
        else State.selected.add(dir);
      } else {
        State.selected.clear();
        State.selected.add(dir);
      }
      card.querySelectorAll('tr[data-dir]').forEach(t => {
        t.classList.toggle('sel', State.selected.has(t.dataset.dir));
        const cb = t.querySelector('.jrow-chk');
        if (cb) cb.checked = State.selected.has(t.dataset.dir);
      });
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
        const ok = await VCS.confirm('未知主机指纹:' + (res.message || '') +
          '\n\n信任该主机并重试?');
        if (!ok) { VCS.log('已取消(未信任主机)', 'failc'); return null; }
        trust = true;
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
  }

  // ── 上传并提交 ─────────────────────────────────────────────────────────────
  async function doSubmit() {
    const name = requireProfile();
    if (!name) return;
    const dirs = selectedDirs();
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

  // ── 拉回结果:预设弹窗(轻量 / DOS·Bader 全家桶 / 自定义) ─────────────────
  function askFetchFiles(n) {
    return new Promise(resolve => {
      const presets = [
        ['轻量(默认):CONTCAR + OSZICAR + OUTCAR', ['CONTCAR', 'OSZICAR', 'OUTCAR']],
        ['DOS/Bader 全家桶:轻量 + vasprun.xml + DOSCAR + CHGCAR + AECCAR0/2(大文件,较慢)',
          ['CONTCAR', 'OSZICAR', 'OUTCAR', 'vasprun.xml', 'DOSCAR', 'CHGCAR', 'AECCAR0', 'AECCAR2']],
      ];
      let done = false;
      const finish = v => { if (!done) { done = true; resolve(v); } };
      const body =
        `<label style="display:block;margin-bottom:8px"><input type="radio" name="fp" value="0" checked> ${VCS.esc(presets[0][0])}</label>` +
        `<label style="display:block;margin-bottom:8px"><input type="radio" name="fp" value="1"> ${VCS.esc(presets[1][0])}</label>` +
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
              mm.close(); finish(files);
            } },
        ],
      });
      m.onDismiss = () => finish(null);
    });
  }

  async function doFetch() {
    const name = requireProfile();
    if (!name) return;
    const dirs = selectedDirs();
    if (!dirs.length) { VCS.log('请先选中要拉回结果的作业(通常是 DONE/未收敛 的)', 'failc'); return; }
    const files = await askFetchFiles(dirs.length);
    if (!files) return;
    VCS.log(`拉回 ${dirs.length} 个作业的 ${files.join('、')}…`);
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
    const dirs = selectedDirs();
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
  function autoTick() { runStatus(true); }

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
    const rows = visibleRows();
    const allSel = rows.length && rows.every(r => State.selected.has(r.dir));
    if (allSel) rows.forEach(r => State.selected.delete(r.dir));
    else rows.forEach(r => State.selected.add(r.dir));
    renderTable();
  }
  async function batchCancel() {
    const dirs = selectedDirs();
    if (!dirs.length) { VCS.log('批量取消:请先勾选要取消的作业', 'failc'); return; }
    const name = requireProfile();
    if (!name) return;
    if (!await VCS.confirm('确认取消勾选的 ' + dirs.length + ' 个作业?(qdel/scancel + 台账标记 FAILED/用户取消)')) return;
    const res = await remote(name, (pw, trust) => VCS.call('jobs_cancel_batch', dirs, name, pw, trust));
    if (!res) return;
    if (res.error) { VCS.log('批量取消失败:' + res.error, 'failc'); return; }
    (res.cancelled || []).forEach(j => VCS.log('已取消作业号 ' + j, 'okc'));
    (res.failed || []).forEach(f => VCS.log('取消失败 ' + f.job_id + ':' + f.reason, 'failc'));
    await reload();
  }

  // ── 快速批量提交(任意输入文件建作业) ──
  const QS = { files: [] };
  function renderQsFiles() {
    const box = $('#qs-filelist');
    const cnt = $('#qs-count');
    if (cnt) cnt.textContent = QS.files.length + ' 项';
    if (!box) return;
    box.innerHTML = QS.files.map((f, i) =>
      `<span class="qs-file" data-i="${i}">${VCS.esc(f)} <b data-rm="${i}">×</b></span>`).join('') ||
      '<span class="sub">尚未添加目录或文件</span>';
  }
  async function qsAdd() {
    const r = await VCS.call('pick_file', 'input');
    if (r && r.error) { VCS.log('选择文件失败:' + r.error, 'failc'); return; }
    if (r && r.path && QS.files.indexOf(r.path) < 0) { QS.files.push(r.path); renderQsFiles(); }
  }
  async function qsAddVaspDir() {
    const r = await VCS.call('pick_dir');
    if (r && r.error) { VCS.log('选择 VASP 目录失败:' + r.error, 'failc'); return; }
    if (r && r.path && QS.files.indexOf(r.path) < 0) {
      QS.files.push(r.path);
      renderQsFiles();
      VCS.log('已添加 VASP 四件套目录:' + r.path, 'okc');
    }
  }
  async function qsBuild() {
    if (!QS.files.length) { VCS.log('快速提交:请先添加 VASP 目录或输入文件', 'failc'); return; }
    const out = ($('#qs-out') && $('#qs-out').value.trim()) || '';
    if (!out) { VCS.log('快速提交:请选择输出根目录', 'failc'); return; }
    VCS.log('导入已有输入并建作业(' + QS.files.length + ' 项)…');
    const r = await VCS.call('quick_submit_build', QS.files, out, '');
    if (!r || r.ok === false || r.error) {
      VCS.log('快速提交失败:' + ((r && r.error) || '未知错误'), 'failc'); return;
    }
    (r.jobs || []).forEach(j => {
      VCS.log('已建作业:' + j.name + '(' + j.engine + ')→ ' + j.dir, 'okc');
      if (j.hint) VCS.log('  提交命令模板:' + j.hint);
    });
    (r.skipped || []).forEach(s => VCS.log('跳过 ' + s.file + ':' + s.reason, 'warnc'));
    VCS.log('已建 ' + (r.jobs || []).length + ' 个作业并入台账,勾选后可上传提交', 'okc');
    VCS.toast('已建 ' + (r.jobs || []).length + ' 个作业');
    await reload();
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
    wire('jb-submit', doSubmit);
    wire('jb-status', () => runStatus(false));
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
    wire('qs-clearfiles', () => { QS.files = []; renderQsFiles(); });
    wire('qs-out-btn', async () => {
      const r = await VCS.call('pick_dir');
      if (r && r.path && $('#qs-out')) $('#qs-out').value = r.path;
    });
    wire('qs-build', qsBuild);
    const qsl = $('#qs-filelist');
    if (qsl) qsl.addEventListener('click', e => {
      const rm = e.target.closest('[data-rm]');
      if (rm) { QS.files.splice(parseInt(rm.dataset.rm, 10), 1); renderQsFiles(); }
    });
    renderQsFiles();
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
    if (psel) psel.addEventListener('change', () =>
      localStorage.setItem(PROFILE_KEY, currentProfile()));
    bindTable();
    reload();
  }

  // 切回作业页时刷新台账
  document.addEventListener('vcs:page', e => { if (e.detail && e.detail.page === 'jobs') reload(); });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // 供集群页(Task 6)保存后调用,刷新下拉/台账
  window.Jobs = { reload };
})();
