// cluster.js — 集群页:多 profile 表单 + 测试连接(含调度器探测不一致警告)+ 提交脚本预览。
// 只依赖 app.js 暴露的 VCS.* 与 api 桥方法。字段与 ClusterProfile 全量对齐。
// 行为对齐 vcstudio/gui/cluster_tab.py(save-first 语义、password 只经 keyring)。
// 全部数据插值走 VCS.esc(注入防御);零 emoji;中文文案。密码绝不落 yaml。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const State = { profiles: {} };   // name -> profile dict

  // ── 表单读写 ────────────────────────────────────────────────────────────────
  function radio(name) {
    const el = document.querySelector(`input[name="${name}"]:checked`);
    return el ? el.value : '';
  }
  function setRadio(name, value) {
    document.querySelectorAll(`input[name="${name}"]`).forEach(r => { r.checked = r.value === value; });
  }
  function num(id, def) {
    const n = parseInt(($(id) && $(id).value || '').trim(), 10);
    return Number.isNaN(n) ? def : n;
  }
  function trimval(id) { return ($(id) && $(id).value || '').trim(); }

  // 表单现值 → save_profile 所需 dict(数字化 + env 按行去空)
  function readForm() {
    return {
      name: trimval('cl-name'),
      hostname: trimval('cl-hostname'),
      port: num('cl-port', 22),
      username: trimval('cl-username'),
      auth: radio('cl-auth') || 'key',
      key_path: trimval('cl-keypath'),
      use_jump: !!($('cl-usejump') && $('cl-usejump').checked),
      jump_host: trimval('cl-jumphost'),
      jump_user: trimval('cl-jumpuser'),
      jump_port: num('cl-jumpport', 22),
      remote_root: trimval('cl-remoteroot'),
      scheduler: trimval('cl-scheduler') || 'Slurm',
      scheduler_bin: trimval('cl-schedbin'),
      queue: trimval('cl-queue'),
      nodes: num('cl-nodes', 1),
      ppn: num('cl-ppn', 0),
      walltime: trimval('cl-walltime') || '24:00:00',
      env_lines: ($('cl-env') && $('cl-env').value || '')
        .split(/\r?\n/).map(s => s.trim()).filter(Boolean),
      vasp_cmd: ($('cl-vaspcmd') && $('cl-vaspcmd').value || '').trim(),
      script_mode: radio('cl-mode') || 'auto',
      template_path: trimval('cl-template'),
    };
  }

  // profile dict → 回填全部字段
  function fillForm(p) {
    p = p || {};
    const set = (id, v) => { const el = $(id); if (el) el.value = v; };
    set('cl-name', p.name || '');
    set('cl-hostname', p.hostname || '');
    set('cl-port', String(p.port != null ? p.port : 22));
    set('cl-username', p.username || '');
    setRadio('cl-auth', p.auth || 'key');
    set('cl-keypath', p.key_path || '');
    if ($('cl-usejump')) $('cl-usejump').checked = !!p.use_jump;
    set('cl-jumphost', p.jump_host || '');
    set('cl-jumpuser', p.jump_user || '');
    set('cl-jumpport', String(p.jump_port != null ? p.jump_port : 22));
    set('cl-remoteroot', p.remote_root || '');
    set('cl-scheduler', p.scheduler || 'Slurm');
    set('cl-schedbin', p.scheduler_bin || '');
    set('cl-queue', p.queue || '');
    set('cl-nodes', String(p.nodes != null ? p.nodes : 1));
    set('cl-ppn', p.ppn ? String(p.ppn) : '');    // 0 = 未设置,显示空(同 cluster_tab.py)
    set('cl-walltime', p.walltime || '24:00:00');
    set('cl-env', (p.env_lines || []).join('\n'));
    set('cl-vaspcmd', p.vasp_cmd || '');
    setRadio('cl-mode', p.script_mode || 'auto');
    set('cl-template', p.template_path || '');
    toggleKeyRow();
    hideWarn();
  }

  function newProfile() {
    fillForm({ name: '', port: 22, jump_port: 22, nodes: 1, walltime: '24:00:00',
               auth: 'key', scheduler: 'Slurm', script_mode: 'auto', env_lines: [] });
    if ($('cl-profile')) $('cl-profile').value = '';
    if ($('cl-name')) $('cl-name').focus();
  }

  // auth=password 时隐藏密钥文件行
  function toggleKeyRow() {
    const row = $('cl-keyrow');
    if (row) row.hidden = (radio('cl-auth') !== 'key');
  }

  // ── 警告条 ──────────────────────────────────────────────────────────────────
  function showWarn(msg) { const el = $('cl-warn'); if (el) { el.textContent = msg; el.hidden = false; } }
  function hideWarn() { const el = $('cl-warn'); if (el) el.hidden = true; }

  // ── profile 下拉 ────────────────────────────────────────────────────────────
  async function loadProfiles(keepName) {
    const r = await VCS.call('list_profiles');
    const profs = (r && r.profiles) || [];
    State.profiles = {};
    profs.forEach(p => { State.profiles[p.name] = p; });
    const sel = $('cl-profile');
    if (sel) {
      const prev = keepName || sel.value;
      sel.innerHTML = '';
      if (!profs.length) {
        const o = document.createElement('option');
        o.value = ''; o.textContent = '(无,点新建)';
        sel.appendChild(o);
      } else {
        profs.forEach(p => {
          const o = document.createElement('option');
          o.value = p.name; o.textContent = p.name;
          sel.appendChild(o);
        });
        sel.value = profs.some(p => p.name === prev) ? prev : profs[0].name;
      }
    }
    if (r && r.error) VCS.log('读取集群配置失败:' + r.error, 'failc');
    return profs;
  }

  function currentName() { return $('cl-profile') ? $('cl-profile').value : ''; }

  // ── 保存 ────────────────────────────────────────────────────────────────────
  async function save() {
    const data = readForm();
    if (!data.name) { VCS.log('集群名称不能为空', 'failc'); return false; }
    const r = await VCS.call('save_profile', data);
    if (!(r && r.ok)) { VCS.log('保存失败:' + ((r && r.error) || '未知错误'), 'failc'); return false; }
    await loadProfiles(data.name);
    VCS.log(`已保存集群「${data.name}」(密码不写入 yaml)`, 'okc');
    // 同步任务页集群下拉 + 侧栏底部默认集群(若已加载)
    if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    if (typeof VCS.refreshNavFoot === 'function') VCS.refreshNavFoot();
    return true;
  }

  // ── 删除 ────────────────────────────────────────────────────────────────────
  async function deleteProfile() {
    const name = currentName();
    if (!name || !State.profiles[name]) { VCS.log('没有可删除的集群', 'failc'); return; }
    const ok = await VCS.confirm(`删除集群「${name}」?`);
    if (!ok) return;
    const r = await VCS.call('delete_profile', name);
    if (!(r && r.ok)) { VCS.log('删除失败:「' + name + '」', 'failc'); return; }
    VCS.log(`已删除集群「${name}」`, 'okc');
    const profs = await loadProfiles();
    if (profs.length) fillForm(State.profiles[currentName()]);
    else newProfile();
    if (window.Jobs && typeof window.Jobs.reload === 'function') window.Jobs.reload();
    if (typeof VCS.refreshNavFoot === 'function') VCS.refreshNavFoot();
  }

  // ── 测试连接:先保存现值 → 取密码(仅 password 且 keyring 无)→ 测试 → 探测比对 ──
  async function testConnection() {
    hideWarn();  // 清掉上次测试遗留的调度器不一致警告条,避免失败/取消后残留
    const data = readForm();
    if (!data.name) { VCS.log('集群名称不能为空,无法测试', 'failc'); return; }
    // save-first:test_connection 读的是已保存 profile,故先落盘
    if (!(await save())) return;

    // 密码认证且凭据库无密码 → 弹框现输(密码只经 keyring,绝不落 yaml)
    let password = null;
    if (data.auth === 'password') {
      const hp = await VCS.call('has_saved_password', data.name);
      if (!(hp && hp.saved)) {
        password = await VCS.password();
        if (password === null) { VCS.log('已取消(未输入密码)', 'failc'); return; }
      }
    }

    VCS.log(`测试连接「${data.name}」…`);
    let trust = false;
    for (;;) {
      const res = await VCS.call('test_connection', data.name, password, trust);
      if (!res) { VCS.log('测试连接无返回', 'failc'); return; }
      if (res.needs_trust) {
        const ok = await VCS.confirm((res.message || '未知主机指纹') + '\n\n是否信任该主机并重试?');
        if (!ok) { VCS.log('已取消(未信任主机)', 'failc'); return; }
        trust = true;
        continue;
      }
      if (res.ok) {
        VCS.log(res.message || '连接成功', 'okc');
        // 探测调度器 vs 表单选择:不一致 → 黄色警告条 + 自动切下拉(用户可改回再保存)
        const chosen = data.scheduler;
        if (res.scheduler && res.scheduler !== chosen) {
          showWarn(`远端探测到 ${res.scheduler},与当前选择的 ${chosen} 不一致,已为你选中(可改回再保存)`);
          if ($('cl-scheduler')) $('cl-scheduler').value = res.scheduler;
          VCS.log(`探测到调度器 ${res.scheduler}(原选择 ${chosen}),已自动切换下拉;如需保留请改回后重新保存`, 'okc');
        } else {
          hideWarn();
        }
      } else {
        VCS.log(res.message || '连接失败', 'failc');
      }
      return;
    }
  }

  // ── 预览提交脚本:从台账选一个作业目录 → preview_script ──────────────────────
  async function previewScript() {
    const name = currentName();
    if (!name || !State.profiles[name]) { VCS.log('请先保存并选择一个集群再预览脚本', 'failc'); return; }
    const r = await VCS.call('list_jobs');
    const jobs = (r && r.jobs) || [];
    if (!jobs.length) { VCS.log('台账为空,先在生成页产出一个作业', 'failc'); return; }

    const opts = jobs.map(j =>
      `<option value="${VCS.esc(j.dir)}">${VCS.esc(j.name)} — ${VCS.esc(j.dir)}</option>`).join('');
    const body =
      `<div class="sub" style="margin-bottom:6px">选择一个台账作业目录作为示例(所见即所交)</div>` +
      `<select id="cl-prevdir" class="ipt">${opts}</select>` +
      `<pre class="cl-pre" id="cl-prevtext" style="margin-top:12px;max-height:46vh">正在生成…</pre>`;

    const m = VCS.modal({
      title: `预览提交脚本 — ${VCS.esc(name)}`,
      bodyHTML: body,
      actions: [{ label: '关闭', onClick: mm => mm.close() }],
    });

    const render = async () => {
      const dir = m.el.querySelector('#cl-prevdir').value;
      const pre = m.el.querySelector('#cl-prevtext');
      pre.textContent = '正在生成…';
      const pr = await VCS.call('preview_script', name, dir);
      pre.textContent = (pr && pr.ok) ? (pr.text || '(空脚本)')
                                      : ('预览失败:' + ((pr && pr.error) || '未知错误'));
    };
    m.el.querySelector('#cl-prevdir').addEventListener('change', render);
    render();
  }

  // ── 初始化 ──────────────────────────────────────────────────────────────────
  function wire(id, fn) { const el = $(id); if (el) el.addEventListener('click', fn); }

  async function init() {
    wire('cl-new', newProfile);
    wire('cl-delete', deleteProfile);
    wire('cl-save', save);
    wire('cl-test', testConnection);
    wire('cl-preview', previewScript);
    const sel = $('cl-profile');
    if (sel) sel.addEventListener('change', () => {
      const p = State.profiles[sel.value];
      if (p) fillForm(p); else newProfile();
    });
    document.querySelectorAll('input[name="cl-auth"]').forEach(r =>
      r.addEventListener('change', toggleKeyRow));

    const profs = await loadProfiles();
    if (profs.length) fillForm(State.profiles[currentName()] || profs[0]);
    else newProfile();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.Cluster = { reload: () => loadProfiles() };
})();
