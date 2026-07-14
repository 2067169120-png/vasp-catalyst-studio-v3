// jobs.js — 任务页:台账表 + 全部批量动作 + 密码/信任流 + 自动刷新 + 集群队列/认领。
// 只依赖 app.js 暴露的 VCS.* 与 api 桥方法。行为逐条对齐 vcstudio/gui/jobs_tab.py。
// 全部数据插值走 VCS.esc(注入防御);零 emoji;中文文案。
'use strict';
(function () {
  const PROFILE_KEY = 'vcs.jobs.profile';
  const $ = sel => document.querySelector(sel);

  const State = {
    rows: [],            // list_jobs 返回的行
    stale: [],           // 失效条目目录
    profiles: {},        // name -> profile dict
    selected: new Set(), // 选中的 dir
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
    renderTable();
    renderStats();
    renderStale();
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

    let h = '<table><thead><tr>' +
      '<th>作业</th><th>状态</th><th class="mono">作业号</th>' +
      '<th class="num">步</th><th class="num">|F|max</th><th class="num">E0 (eV)</th>' +
      '<th>诊断</th><th class="mono">更新</th></tr></thead><tbody>';
    State.rows.forEach(r => {
      const task = r.task && r.task !== '/' ? r.task : '';
      const sel = State.selected.has(r.dir) ? ' class="sel"' : '';
      h += `<tr data-dir="${VCS.esc(r.dir)}"${sel}>` +
        `<td><span class="name">${VCS.esc(r.name)}</span>` +
        (task ? ` <span class="sub">${VCS.esc(task)}</span>` : '') + '</td>' +
        `<td>${VCS.pill(r.state)}</td>` +
        `<td class="mono">${r.job_id ? VCS.esc(r.job_id) : '—'}</td>` +
        `<td class="num">${r.steps != null ? VCS.esc(r.steps) : '—'}</td>` +
        `<td class="num">${r.fmax != null ? VCS.esc(r.fmax) : '—'}</td>` +
        `<td class="num">${r.energy !== '' && r.energy != null ? VCS.esc(r.energy) : '—'}</td>` +
        `<td class="diag">${VCS.esc(r.diag || '')}</td>` +
        `<td class="mono">${VCS.esc(r.updated || '')}</td></tr>`;
    });
    h += '</tbody></table>';
    card.innerHTML = h;
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
      const tr = e.target.closest('tr[data-dir]');
      if (!tr) return;
      const dir = tr.dataset.dir;
      if (e.ctrlKey || e.metaKey) {
        if (State.selected.has(dir)) State.selected.delete(dir);
        else State.selected.add(dir);
      } else {
        State.selected.clear();
        State.selected.add(dir);
      }
      card.querySelectorAll('tr[data-dir]').forEach(t =>
        t.classList.toggle('sel', State.selected.has(t.dataset.dir)));
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
      if (res.error) { VCS.log((auto ? '自动刷新' : '查询') + '异常:' + res.error, 'failc'); return; }
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
    showQueueModal(name, res.jobs || []);
  }

  function showQueueModal(name, jobs) {
    const known = new Set(State.rows.filter(r => r.job_id).map(r => String(r.job_id)));
    if (!jobs.length) VCS.log(`「${name}」队列为空(该用户当前没有在队/在跑作业)`);
    let h = '<div style="max-height:52vh;overflow:auto"><table><thead><tr>' +
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
      '纳入台账,之后可查状态/拉回/续算。</p>';
    const m = VCS.modal({
      title: `集群队列 — ${name}`,
      bodyHTML: h,
      actions: [{ label: '关闭', onClick: mm => mm.close() }],
    });
    m.el.querySelectorAll('button[data-claim]').forEach(btn => {
      btn.addEventListener('click', () => {
        const j = jobs[+btn.dataset.claim];
        askAdopt(name, j, () => { if (btn.parentNode) btn.parentNode.textContent = '已纳管'; });
      });
    });
  }

  function askAdopt(name, j, onDone) {
    const body =
      `<div style="margin-bottom:10px"><div class="sub" style="margin-bottom:4px">远程目录(绝对路径,以 / 开头)</div>` +
      `<input id="ad-remote" class="ipt" value="${VCS.esc(j.workdir || '')}" placeholder="/home/Maple123/..."></div>` +
      `<div><div class="sub" style="margin-bottom:4px">本地目录(结果将拉回到这里;不存在会新建)</div>` +
      `<input id="ad-local" class="ipt" placeholder="例如 E:\\runs\\claimed_${VCS.esc(j.job_id)}"></div>`;
    VCS.modal({
      title: `认领作业 ${VCS.esc(j.job_id)}${j.name ? ' · ' + VCS.esc(j.name) : ''}`,
      bodyHTML: body,
      actions: [
        { label: '取消', quiet: true, onClick: mm => mm.close() },
        { label: '认领', primary: true, onClick: async mm => {
            const remote = mm.el.querySelector('#ad-remote').value.trim();
            const local = mm.el.querySelector('#ad-local').value.trim();
            if (!local) { VCS.toast('请填写本地目录', 'fail'); return; }
            if (!remote) { VCS.toast('请填写远程目录', 'fail'); return; }
            mm.close();
            const r = await VCS.call('adopt_job', local, name, String(j.job_id), remote, j.name || '');
            if (r && r.error) { VCS.log('认领失败:' + r.error, 'failc'); return; }
            VCS.log(`已认领作业 ${j.job_id} → ${local}(下次「查询状态」即可追踪)`, 'okc');
            if (onDone) onDone();
            await reload();
          } },
      ],
    });
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

  function applyAuto() {
    const cb = $('#jb-auto');
    const sel = $('#jb-interval');
    if (State.autoTimer) { clearInterval(State.autoTimer); State.autoTimer = null; }
    if (cb && cb.checked) {
      const mins = parseInt(sel.value, 10) || 15;
      State.autoTimer = setInterval(autoTick, mins * 60000);
      VCS.log(`自动刷新开启(每 ${mins} 分钟)`);
    } else {
      VCS.log('自动刷新已关闭');
    }
  }

  // ── 初始化 ─────────────────────────────────────────────────────────────────
  function wire(id, fn) { const el = $('#' + id); if (el) el.addEventListener('click', fn); }

  function init() {
    wire('jb-submit', doSubmit);
    wire('jb-status', () => runStatus(false));
    wire('jb-fetch', doFetch);
    wire('jb-continue', doContinue);
    wire('jb-queue', doQueue);
    wire('jb-open', doOpen);
    wire('jb-remove', doRemove);
    wire('jb-clean', doClean);
    const cb = $('#jb-auto');
    const iv = $('#jb-interval');
    if (cb) cb.addEventListener('change', applyAuto);
    if (iv) iv.addEventListener('change', () => { if (cb && cb.checked) applyAuto(); });
    const psel = $('#jobs-profile');
    if (psel) psel.addEventListener('change', () =>
      localStorage.setItem(PROFILE_KEY, currentProfile()));
    bindTable();
    reload();
  }

  // 切回任务页时刷新台账
  document.addEventListener('vcs:page', e => { if (e.detail && e.detail.page === 'jobs') reload(); });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // 供集群页(Task 6)保存后调用,刷新下拉/台账
  window.Jobs = { reload };
})();
