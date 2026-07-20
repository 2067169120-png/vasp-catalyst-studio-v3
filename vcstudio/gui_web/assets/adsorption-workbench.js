// adsorption-workbench.js — 吸附能优先工作台：整文件夹扫描、角色确认、导入与直接提交。
// 科学分类由 project/result_import.py 决定；前端只展示证据并收集用户显式选择。
'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const ROLE_LABEL = {
    clean: '清洁表面', config: '吸附构型', ref: '单一气相参考',
    molecule: '物种参考态', ignore: '不导入',
  };
  const TASKS = ['relax', 'static', 'freq', 'dos', 'band'];

  function pathParts(path) {
    const value = String(path || '').replace(/[\\/]+$/, '');
    const index = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'));
    const parent = index > 0 ? value.slice(0, index) : (index === 0 ? '/' : '.');
    return { parent,
      base: index >= 0 ? value.slice(index + 1) : value };
  }

  function joinPath(parent, child) {
    const p = String(parent || '').replace(/[\\/]+$/, '');
    const sep = p.indexOf('\\') >= 0 ? '\\' : '/';
    return p + sep + child;
  }

  function safeProjectName(value) {
    return String(value || 'imported-project')
      .replace(/[^A-Za-z0-9_.\-\u4e00-\u9fff]+/g, '_').replace(/^[_\.]+|[_\.]+$/g, '') ||
      'imported-project';
  }

  function setAdsorptionView() {
    const select = $('analysis-type');
    if (select) {
      select.value = 'adsorption';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  async function showProjectPage(focusSelector) {
    if (typeof VCS.navigate === 'function') {
      await VCS.navigate('project', { focusSelector: focusSelector || '', source: 'adsorption' });
    } else {
      const link = document.querySelector('nav a[data-page="project"]');
      if (link) link.click();
    }
    setAdsorptionView();
  }

  function option(value, label, selected) {
    return `<option value="${VCS.esc(value)}"${selected ? ' selected' : ''}>` +
      `${VCS.esc(label)}</option>`;
  }

  function roleOptions(value, projectKind) {
    if (projectKind === 'molecule_library') {
      return option('', '需确认', !value) +
        option('ignore', '不导入', value === 'ignore') +
        option('molecule', '锂硫/物种参考态', value === 'molecule');
    }
    return option('', '需确认', !value) +
      option('ignore', '不导入', value === 'ignore') +
      option('clean', '清洁表面（只能 1 个）', value === 'clean') +
      option('config', '吸附构型（至少 1 个）', value === 'config') +
      option('ref', '单一气相参考', value === 'ref') +
      option('molecule', '锂硫/物种参考态', value === 'molecule');
  }

  function taskOptions(value) {
    let html = option('', value && value !== 'unknown' ? `自动：${value}` : '自动识别', true);
    TASKS.forEach(task => { html += option(task, task, false); });
    return html;
  }

  function steps(current) {
    const labels = ['选文件夹', '确认角色', '预检与资源', '提交或分析'];
    return '<div class="ri-steps">' + labels.map((label, index) => {
      const step = index + 1;
      const cls = step < current ? 'done' : (step === current ? 'current' : '');
      return `<span class="${cls}"><b>${step}</b>${VCS.esc(label)}</span>` +
        (index < labels.length - 1 ? '<i></i>' : '');
    }).join('') + '</div>';
  }

  function alertHtml(messages, kind) {
    const list = (messages || []).filter(Boolean);
    if (!list.length) return '';
    return `<div class="ri-alert ${kind}">` +
      list.map(message => `<div>${VCS.esc(message)}</div>`).join('') + '</div>';
  }

  function stateEvidence(candidate) {
    const diagnosis = candidate.diagnosis || {};
    const issues = ((candidate.files || {}).input_issues || []);
    const pieces = [candidate.role_reason, diagnosis.evidence];
    if (issues.length) pieces.push('输入检查：' + issues.join('；'));
    return pieces.filter(Boolean).join(' · ');
  }

  function energyText(candidate) {
    const trusted = candidate.energy_e0_eV;
    const raw = candidate.raw_energy_e0_eV;
    const observed = candidate.observed_energy_eV;
    if (typeof trusted === 'number') return trusted.toFixed(6) + ' eV';
    if (typeof raw === 'number') return raw.toFixed(6) + ' eV（待核验）';
    if (typeof observed === 'number') {
      return observed.toFixed(6) + ' eV（非 E0）';
    }
    return '—';
  }

  async function acquirePassword(profile) {
    if (!profile || profile.auth !== 'password') return { ok: true, password: null };
    const saved = await VCS.call('has_saved_password', profile.name);
    if (saved && saved.saved) return { ok: true, password: null };
    const password = await VCS.password();
    return password === null ? { ok: false, password: null } : { ok: true, password };
  }

  async function remote(profile, invoke) {
    const auth = await acquirePassword(profile);
    if (!auth.ok) return null;
    let password = auth.password;
    let trust = false;
    let result = await invoke(password, trust);
    for (;;) {
      if (!result || result.cancelled) return null;
      if (result.needPassword) {
        password = result.password;
        result = await invoke(password, trust);
        continue;
      }
      if (result.needs_trust) {
        const yes = await VCS.confirm('检测到未知主机指纹：' + (result.message || '') +
          '\n\n确认信任该主机并继续？');
        if (!yes) return null;
        trust = true;
        result = await invoke(password, trust);
        continue;
      }
      return result;
    }
  }

  function openWizard(scan, mode, profiles) {
    const candidates = (scan && (scan.preview || scan.candidates)) || [];
    const parts = pathParts(scan.root);
    const projectKind = mode === 'molecules' ? 'molecule_library' : 'adsorption';
    const model = {
      mode,
      projectKind,
      name: safeProjectName(parts.base + (projectKind === 'molecule_library' ? '-molecules' : '')),
      outputRoot: joinPath(parts.parent, 'VCS-Managed'),
      includeLarge: false,
      phase: 'map',
      plan: null,
      profiles: profiles || [],
      selectedProfile: '',
      errors: [],
      rows: candidates.map(candidate => ({
        candidate,
        role: projectKind === 'molecule_library'
          ? ((candidate.role_suggestion === 'molecule' || candidate.species_suggestion)
            ? 'molecule' : '')
          : (candidate.role_suggestion || ''),
        species: candidate.species_suggestion || '',
        taskType: '',
        manualConfirm: false,
        forceCreated: false,
      })),
      assignments: [],
    };
    const savedProfile = (() => {
      try { return localStorage.getItem('vcs.jobs.profile') || ''; } catch (_) { return ''; }
    })();
    model.selectedProfile = model.profiles.some(p => p.name === savedProfile)
      ? savedProfile : (model.profiles[0] ? model.profiles[0].name : '');

    const body = document.createElement('div');
    let primaryButton = null;
    const modalTitle = projectKind === 'molecule_library' ? '导入锂硫 / 分子参考库'
      : (mode === 'inputs' ? '导入四件套并直接提交' : '导入已有吸附能结果');
    const modal = VCS.modal({
      title: modalTitle,
      body,
      actions: [
        { label: '取消', quiet: true, onClick: current => current.close() },
        { label: '重新选择文件夹', onClick: current => goBackOrReselect(current) },
        { label: '预检导入', primary: true, onClick: () => advance() },
      ],
    });
    modal.el.classList.add('ads-import-modal');
    primaryButton = modal.el.querySelector('.m-actions .btn.primary');
    const actionButtons = modal.el.querySelectorAll('.m-actions .btn');
    const secondaryButton = actionButtons.length > 1 ? actionButtons[1] : null;

    function goBackOrReselect(current) {
      if (model.phase === 'preview') {
        model.phase = 'map';
        model.plan = null;
        model.errors = [];
        renderMap();
        return;
      }
      current.close();
      openImport(mode);
    }

    function setPrimary(label, disabled) {
      if (!primaryButton) return;
      primaryButton.textContent = label;
      primaryButton.disabled = !!disabled;
    }

    function setSecondary(label) {
      if (secondaryButton) secondaryButton.textContent = label;
    }

    function renderMap() {
      const rows = model.rows.map((row, index) => {
        const c = row.candidate;
        const ambiguous = !row.role ? ' ambiguous' : '';
        const needs = c.state_suggestion === 'NEEDS_HUMAN' ? ' needs' : '';
        const canConfirm = c.state_suggestion === 'NEEDS_HUMAN' && c.has_output;
        const canRecalculate = !!(c.input_complete && c.source_has_output);
        const confirmation = canConfirm
          ? `<label title="仅确认缺失的收敛标记；物理异常和 NELM 门控仍不能绕过"><input type="checkbox" data-confirm${row.manualConfirm ? ' checked' : ''}${row.forceCreated ? ' disabled' : ''}> 我已核验</label>`
          : '';
        const recalculate = canRecalculate
          ? `<label title="忽略源目录里的旧结果，只复制验证通过的四件套并作为新任务"><input type="checkbox" data-force${row.forceCreated ? ' checked' : ''}> 只用四件套重算</label>`
          : '';
        return `<tr class="${ambiguous}${needs}" data-index="${index}">` +
          `<td><select data-role>${roleOptions(row.role, model.projectKind)}</select></td>` +
          `<td class="ri-name">${VCS.esc(c.relative_path || c.name)}</td>` +
          `<td><select data-task>${taskOptions(c.task_type)}</select></td>` +
          `<td>${VCS.pill(c.state_suggestion || 'NEEDS_HUMAN')}` +
          `<div class="sub">${VCS.esc(energyText(c))}</div></td>` +
          `<td><input type="text" data-species value="${VCS.esc(row.species)}" ` +
          `placeholder="如 Li2S4"${row.role === 'molecule' || row.role === 'config' ? '' : ' disabled'}></td>` +
          `<td>${confirmation}${confirmation && recalculate ? '<br>' : ''}${recalculate || (!confirmation ? '—' : '')}</td>` +
          `<td class="ri-evidence" title="${VCS.esc(stateEvidence(c))}">${VCS.esc(stateEvidence(c))}</td></tr>`;
      }).join('');
      const scanWarnings = [...(scan.warnings || []), ...model.errors];
      body.innerHTML = steps(2) +
        `<div class="ri-head"><div><h3>已找到 ${candidates.length} 个 VASP 计算目录</h3>` +
        `<div class="ri-path">${VCS.esc(scan.root)}</div></div>` +
        `<span class="ri-badge">源目录只读</span></div>` +
        alertHtml(scanWarnings, model.errors.length ? 'fail' : 'warn') +
        '<div class="ri-form">' +
        `<label class="ri-field"><span>项目名</span><input class="ipt" id="ri-name" value="${VCS.esc(model.name)}"></label>` +
        `<label class="ri-field"><span>受管输出根目录（自动复制，不覆盖源文件）</span><input class="ipt" id="ri-output" value="${VCS.esc(model.outputRoot)}"></label>` +
        '<button class="btn" id="ri-output-pick">更改目录</button></div>' +
        `<div class="ri-alert warn">${model.projectKind === 'molecule_library'
          ? '参考库模式：至少选择 1 个“物种参考态”，每个都必须确认化学式；不会要求清洁面或吸附构型。'
          : '角色要求：必须且只能有 1 个清洁表面，至少有 1 个吸附构型；一旦加入物种参考态，每个吸附构型都必须明确填写对应物种。'}</div>` +
        '<div class="ri-table-wrap"><table class="ri-table"><thead><tr>' +
        '<th>体系角色</th><th>计算目录</th><th>任务类型</th><th>检测状态 / E0</th>' +
        '<th>物种</th><th>核验 / 重算</th><th>识别依据</th></tr></thead>' +
        `<tbody>${rows}</tbody></table></div>` +
        '<div class="ri-options"><label><input type="checkbox" id="ri-large"' +
        (model.includeLarge ? ' checked' : '') + '> 同时复制大结果文件（vasprun.xml / CHGCAR / WAVECAR 等，默认关闭）</label>' +
        '<span class="sub">黄色行不会自动成为正式 DONE；所有未确认角色都必须选择或明确“不导入”。</span></div>';
      setPrimary('预检导入', false);
      setSecondary('重新选择文件夹');
      wireMap();
    }

    function wireMap() {
      const name = $('ri-name');
      const output = $('ri-output');
      if (name) name.addEventListener('input', () => { model.name = name.value.trim(); });
      if (output) output.addEventListener('input', () => { model.outputRoot = output.value.trim(); });
      const large = $('ri-large');
      if (large) large.addEventListener('change', () => { model.includeLarge = large.checked; });
      const pick = $('ri-output-pick');
      if (pick) pick.addEventListener('click', async () => {
        const result = await VCS.call('pick_dir');
        if (result && result.path) {
          model.outputRoot = result.path;
          if (output) output.value = result.path;
        }
      });
      body.querySelectorAll('tbody tr[data-index]').forEach(tr => {
        const row = model.rows[Number(tr.dataset.index)];
        const role = tr.querySelector('[data-role]');
        const species = tr.querySelector('[data-species]');
        const task = tr.querySelector('[data-task]');
        const confirm = tr.querySelector('[data-confirm]');
        const force = tr.querySelector('[data-force]');
        role.addEventListener('change', () => {
          row.role = role.value;
          species.disabled = !(row.role === 'molecule' || row.role === 'config');
          tr.classList.toggle('ambiguous', !row.role);
        });
        species.addEventListener('input', () => { row.species = species.value.trim(); });
        task.addEventListener('change', () => { row.taskType = task.value; });
        if (confirm) confirm.addEventListener('change', () => {
          row.manualConfirm = confirm.checked;
        });
        if (force) force.addEventListener('change', () => {
          row.forceCreated = force.checked;
          if (row.forceCreated && confirm) {
            row.manualConfirm = false;
            confirm.checked = false;
          }
          if (confirm) confirm.disabled = row.forceCreated;
        });
      });
    }

    function assignmentsFromMap() {
      const unresolved = model.rows.filter(row => !row.role);
      if (unresolved.length) {
        throw new Error(`还有 ${unresolved.length} 个目录未确认角色；不需要的目录请选择“不导入”`);
      }
      const chosen = model.rows.filter(row => row.role && row.role !== 'ignore');
      if (model.projectKind === 'molecule_library') {
        if (!chosen.length || chosen.some(row => row.role !== 'molecule')) {
          throw new Error('分子参考库至少需要 1 个物种参考态，且不能混入其他角色');
        }
        if (chosen.some(row => !row.species)) {
          throw new Error('每个物种参考态都必须填写化学式');
        }
      }
      const clean = chosen.filter(row => row.role === 'clean').length;
      const configs = chosen.filter(row => row.role === 'config').length;
      if (model.projectKind === 'adsorption') {
        if (clean !== 1) throw new Error('请选择且只选择 1 个清洁表面');
        if (!configs) throw new Error('请至少选择 1 个吸附构型');
        const hasMolecule = chosen.some(row => row.role === 'molecule');
        if (hasMolecule && chosen.some(row => row.role === 'config' && !row.species)) {
          throw new Error('导入物种参考态后，每个吸附构型都必须明确填写对应物种');
        }
      }
      return chosen.map(row => {
        const item = { path: row.candidate.relative_path, role: row.role };
        if (row.species) item.species = row.species;
        if (row.taskType) item.task_type = row.taskType;
        if (row.manualConfirm) item.manual_confirm = true;
        item.force_created = !!row.forceCreated;
        return item;
      });
    }

    async function runPreview() {
      model.errors = [];
      try {
        model.assignments = assignmentsFromMap();
        if (!model.name) throw new Error('请填写项目名');
        if (!model.outputRoot) throw new Error('请选择受管输出根目录');
      } catch (error) {
        model.errors = [error.message || String(error)];
        renderMap();
        return;
      }
      setPrimary('正在预检…', true);
      const result = await VCS.call('proj_import_preview', scan.root, model.name,
        model.outputRoot, model.assignments, model.includeLarge, false, model.projectKind);
      if (!result || result.ok === false || result.error) {
        model.errors = (result && result.errors && result.errors.length)
          ? result.errors : [result && result.error || '导入预检失败'];
        renderMap();
        return;
      }
      model.plan = result;
      model.phase = 'preview';
      renderPlan();
    }

    function profileByName(name) {
      return model.profiles.find(profile => profile.name === name) || null;
    }

    function profileOptions() {
      if (!model.profiles.length) return option('', '(未配置服务器)', true);
      return model.profiles.map(profile => option(profile.name, profile.name,
        profile.name === model.selectedProfile)).join('');
    }

    function profileIssues(profile, resources) {
      if (!profile) return ['未选择服务器'];
      const issues = [];
      if (!String(profile.hostname || '').trim()) issues.push('主机地址');
      if (!String(profile.username || '').trim()) issues.push('用户名');
      const remoteRoot = String(profile.remote_root || '').trim();
      if (!remoteRoot) issues.push('远程工作目录');
      else if (!remoteRoot.startsWith('/')) issues.push('远程工作目录必须是以 / 开头的绝对路径');
      if (profile.script_mode === 'template') {
        if (!String(profile.template_path || '').trim()) issues.push('提交脚本模板');
      } else {
        const queue = resources && Object.prototype.hasOwnProperty.call(resources, 'queue')
          ? resources.queue : profile.queue;
        const ppn = resources && Object.prototype.hasOwnProperty.call(resources, 'ppn')
          ? Number(resources.ppn) : Number(profile.ppn);
        if (!String(queue || '').trim()) issues.push('队列 / 分区');
        if (!Number.isInteger(ppn) || ppn <= 0) issues.push('每节点核数');
        if (!String(profile.vasp_cmd || '').trim()) issues.push('VASP 执行命令');
      }
      return issues;
    }

    function submitBox(created) {
      if (!created) return '';
      if (!model.profiles.length) {
        return '<div class="ri-submit"><h4>下一步：提交未计算项</h4>' +
          '<div class="ri-alert warn">尚未配置集群。可以先完成导入，随后前往“集群”页配置服务器；已算好的结果不受影响。</div></div>';
      }
      const profile = profileByName(model.selectedProfile) || model.profiles[0];
      const issues = profileIssues(profile, {
        queue: profile.queue || '', ppn: profile.ppn,
      });
      const ready = issues.length === 0;
      const direct = model.mode === 'inputs' && ready;
      return '<div class="ri-submit"><h4>像普通提交页一样确认本次计算资源</h4>' +
        `<div class="ri-alert warn" id="ri-profile-warning"${ready ? ' hidden' : ''}>` +
        `服务器配置还缺：${VCS.esc(issues.join('、'))}。请先完善配置，仍可先安全导入。</div>` +
        '<div class="ri-resource-grid">' +
        `<label class="ri-field"><span>服务器</span><select class="ipt" id="ri-profile">${profileOptions()}</select></label>` +
        `<label class="ri-field"><span>队列 / 分区</span><input class="ipt" id="ri-queue" value="${VCS.esc(profile.queue || '')}"></label>` +
        `<label class="ri-field"><span>节点</span><input class="ipt" id="ri-nodes" type="number" min="1" value="${VCS.esc(profile.nodes || 1)}"></label>` +
        `<label class="ri-field"><span>每节点核数</span><input class="ipt" id="ri-ppn" type="number" min="1" value="${VCS.esc(profile.ppn || '')}"></label>` +
        `<label class="ri-field"><span>墙钟时间</span><input class="ipt" id="ri-walltime" value="${VCS.esc(profile.walltime || '24:00:00')}"></label>` +
        '</div>' +
        `<div class="ri-command">计算命令（来自服务器配置，只读）：${VCS.esc(profile.vasp_cmd || '(未配置)')}</div>` +
        '<div class="ri-inline-actions"><button class="btn" id="ri-test-connection">测试连接</button>' +
        '<button class="btn quiet" id="ri-edit-cluster">先导入，稍后配置</button>' +
        `<label><input type="checkbox" id="ri-direct"${direct ? ' checked' : ''}` +
        `${ready ? '' : ' disabled'}> 导入后立即提交 ${created} 个 CREATED 作业</label>` +
        '<span class="sub" id="ri-core-total"></span></div></div>';
    }

    function renderPlan(extraErrors) {
      const plan = model.plan || {};
      const counts = plan.counts || {};
      const memberRows = (plan.members || []).map(member => {
        const warn = (member.warnings || []).join('；');
        return '<tr>' +
          `<td>${VCS.esc(ROLE_LABEL[member.role] || member.role)}</td>` +
          `<td class="ri-name">${VCS.esc(member.relative_path)}</td>` +
          `<td>${VCS.esc(member.task_type || 'unknown')}</td>` +
          `<td>${VCS.pill(member.state)}<div class="sub">${VCS.esc(energyText(member))}</div></td>` +
          `<td class="ri-evidence">复制 ${(member.files && member.files.copy || []).length} 个文件` +
          (warn ? `<br>${VCS.esc(warn)}</td>` : '</td>') + '</tr>';
      }).join('');
      const messages = [...(extraErrors || []), ...(plan.errors || [])];
      body.innerHTML = steps(3) +
        `<div class="ri-head"><div><h3>预检通过，确认后才会复制和登记</h3>` +
        `<div class="ri-path">目标：${VCS.esc(plan.destination || '')}</div></div>` +
        '<span class="ri-badge">原子导入</span></div>' +
        alertHtml(messages, 'fail') + alertHtml(plan.warnings || [], 'warn') +
        '<div class="ri-counts">' +
        `<span>待提交 ${counts.created || 0}</span><span>已完成 ${counts.done || 0}</span>` +
        `<span>需核验 ${counts.needs_human || 0}</span></div>` +
        '<div class="ri-table-wrap"><table class="ri-table"><thead><tr>' +
        '<th>角色</th><th>来源目录</th><th>任务</th><th>采用状态 / E0</th><th>复制与提醒</th>' +
        `</tr></thead><tbody>${memberRows}</tbody></table></div>` +
        submitBox(counts.created || 0);
      wirePlan();
      updatePrimaryForPlan();
      setSecondary('返回修改');
    }

    function updateCoreTotal() {
      const target = $('ri-core-total');
      if (!target) return;
      const nodes = Number(($('ri-nodes') || {}).value || 0);
      const ppn = Number(($('ri-ppn') || {}).value || 0);
      target.textContent = nodes > 0 && ppn > 0 ? `本次共 ${nodes * ppn} 核` : '请填写有效核数';
    }

    function directRequested() {
      const box = $('ri-direct');
      return !!(box && box.checked && model.selectedProfile);
    }

    function updatePrimaryForPlan() {
      const created = Number((model.plan && model.plan.counts || {}).created || 0);
      setPrimary(created && directRequested() ? '确认导入并直接提交' : '确认导入', false);
    }

    function wirePlan() {
      const select = $('ri-profile');
      if (select) select.addEventListener('change', () => {
        model.selectedProfile = select.value;
        const profile = profileByName(select.value);
        if (!profile) return;
        $('ri-queue').value = profile.queue || '';
        $('ri-nodes').value = profile.nodes || 1;
        $('ri-ppn').value = profile.ppn || '';
        $('ri-walltime').value = profile.walltime || '24:00:00';
        const command = body.querySelector('.ri-command');
        if (command) command.textContent = '计算命令（来自服务器配置，只读）：' +
          (profile.vasp_cmd || '(未配置)');
        refreshProfileReadiness(model.mode === 'inputs');
        updateCoreTotal();
        updatePrimaryForPlan();
      });
      ['ri-queue', 'ri-nodes', 'ri-ppn', 'ri-walltime'].forEach(id => {
        const input = $(id); if (input) input.addEventListener('input', () => {
          updateCoreTotal();
          refreshProfileReadiness(false);
          updatePrimaryForPlan();
        });
      });
      const direct = $('ri-direct');
      if (direct) direct.addEventListener('change', updatePrimaryForPlan);
      const edit = $('ri-edit-cluster');
      if (edit) edit.addEventListener('click', () => {
        const direct = $('ri-direct');
        if (direct) direct.checked = false;
        updatePrimaryForPlan();
        VCS.toast('当前角色映射和预检会保留；先导入后再去集群页配置即可');
      });
      const test = $('ri-test-connection');
      if (test) test.addEventListener('click', testConnection);
      updateCoreTotal();
    }

    function currentResourceValues() {
      return {
        queue: String(($('ri-queue') || {}).value || '').trim(),
        ppn: Number(($('ri-ppn') || {}).value || 0),
      };
    }

    function refreshProfileReadiness(selectDirect) {
      const profile = profileByName(model.selectedProfile);
      const issues = profileIssues(profile, currentResourceValues());
      const warning = $('ri-profile-warning');
      if (warning) {
        warning.hidden = issues.length === 0;
        warning.textContent = issues.length
          ? '服务器配置还缺：' + issues.join('、') + '。可先导入，稍后去集群页配置。'
          : '';
      }
      const direct = $('ri-direct');
      if (direct) {
        direct.disabled = issues.length > 0;
        if (issues.length) direct.checked = false;
        else if (selectDirect) direct.checked = true;
      }
      return issues;
    }

    async function testConnection() {
      const profile = profileByName(model.selectedProfile);
      if (!profile) { VCS.toast('请先选择服务器', 'fail'); return; }
      const button = $('ri-test-connection');
      if (button) { button.disabled = true; button.textContent = '测试中…'; }
      try {
        const result = await remote(profile, (password, trust) =>
          VCS.call('test_connection', profile.name, password, trust));
        if (!result) return;
        VCS.toast(result.ok ? '服务器连接成功' : '连接失败：' + (result.message || ''),
          result.ok ? 'ok' : 'fail');
      } finally {
        if (button) { button.disabled = false; button.textContent = '测试连接'; }
      }
    }

    function resourcesFromForm() {
      const nodes = Number(($('ri-nodes') || {}).value || 0);
      const ppn = Number(($('ri-ppn') || {}).value || 0);
      if (!Number.isInteger(nodes) || nodes <= 0) throw new Error('节点数必须是正整数');
      if (!Number.isInteger(ppn) || ppn <= 0) throw new Error('每节点核数必须是正整数');
      const walltime = String(($('ri-walltime') || {}).value || '').trim();
      if (!walltime) throw new Error('墙钟时间不能为空');
      return { queue: String(($('ri-queue') || {}).value || '').trim(),
        nodes, ppn, walltime };
    }

    async function commit() {
      const direct = directRequested();
      let resources = null;
      if (direct) {
        try { resources = resourcesFromForm(); }
        catch (error) { renderPlan([error.message]); return; }
        const localIssues = profileIssues(profileByName(model.selectedProfile), resources);
        if (localIssues.length) {
          renderPlan(['服务器配置还缺：' + localIssues.join('、')]);
          return;
        }
        setPrimary('正在验证服务器配置…', true);
        const profileCheck = await VCS.call(
          'submission_profile_check', model.selectedProfile, resources);
        if (!profileCheck || profileCheck.ok === false || profileCheck.error) {
          const errors = (profileCheck && profileCheck.errors && profileCheck.errors.length)
            ? profileCheck.errors : [profileCheck && profileCheck.error || '服务器提交配置预检失败'];
          renderPlan(errors);
          return;
        }
      }
      setPrimary(direct ? '正在导入并提交…' : '正在导入…', true);
      const result = await VCS.call('proj_import_commit', scan.root, model.name,
        model.outputRoot, model.assignments, model.includeLarge, false,
        model.projectKind, (model.plan && model.plan.fingerprints) || {});
      if (!result || result.ok === false || result.error) {
        renderPlan((result && result.errors && result.errors.length)
          ? result.errors : [result && result.error || '导入失败']);
        return;
      }
      VCS.log('吸附能项目已导入：' + (result.project_path || result.destination), 'okc');
      (result.warnings || []).forEach(warning => VCS.log(warning, 'warnc'));
      if (window.Project && typeof window.Project.reload === 'function') await window.Project.reload();
      if (window.Project && typeof window.Project.selectByPath === 'function') {
        await window.Project.selectByPath(result.project_path || result.destination);
      }
      if (window.Jobs && typeof window.Jobs.reload === 'function') await window.Jobs.reload();

      let submitResult = null;
      const createdDirs = (result.members || [])
        .filter(member => member.state === 'CREATED' && member.input_complete)
        .map(member => member.destination);
      if (direct && createdDirs.length) {
        const profile = profileByName(model.selectedProfile);
        submitResult = await remote(profile, (password, trust) =>
          VCS.call('submit_jobs', createdDirs, profile.name, password, trust, resources));
        if (submitResult && submitResult.error) {
          VCS.log('直接提交失败：' + submitResult.error, 'failc');
        }
        (submitResult && submitResult.results || []).forEach(item => {
          VCS.log(item[0] + '：' + item[2], item[1] ? 'okc' : 'failc');
        });
      }
      modal.close();
      finish(result, createdDirs, submitResult);
    }

    function finish(result, createdDirs, submitResult) {
      const submitted = (submitResult && submitResult.results || []).filter(item => item[1]).length;
      const failed = (submitResult && submitResult.results || []).filter(item => !item[1]).length;
      const done = Number((result.counts || {}).done || 0);
      if (submitResult) {
        const submitError = submitResult.error || '';
        VCS.nextStep({
          title: submitError ? '导入成功，但直接提交失败' :
            (failed ? '导入完成，部分提交需处理' : '导入并提交完成'),
          message: `已导入 ${result.members.length} 个成员；成功提交 ${submitted} 个` +
            (failed ? `，失败 ${failed} 个。` : '。'),
          detail: submitError ? ('提交错误：' + submitError + '。项目和作业已安全导入，可在任务页修正后重试。') :
            (model.projectKind === 'molecule_library'
              ? '下一步：在任务页查看队列和收敛状态；参考态全部 DONE 后会自动供吸附能 / ΔG 报告使用。'
              : '下一步：在任务页查看队列、运行状态和失败原因；全部 DONE 后回吸附能工作台计算 ΔE。'),
          primaryLabel: '查看任务与队列', page: 'jobs', focusJobDir: createdDirs[0] || '',
        });
        return;
      }
      if (createdDirs.length) {
        const noProfile = !model.profiles.length || !model.profiles.some(profile =>
          profileIssues(profile, { queue: profile.queue || '', ppn: profile.ppn }).length === 0);
        VCS.nextStep({
          title: model.projectKind === 'molecule_library' ? '分子参考库已导入' : '吸附能项目已导入',
          message: `已登记 ${createdDirs.length} 个待提交作业` + (done ? `，并采用 ${done} 个已完成结果。` : '。'),
          detail: noProfile ? '下一步：先配置服务器，再回任务页批量提交。' :
            '下一步：选择服务器并批量提交；全部 DONE 后生成报告。',
          primaryLabel: noProfile ? '配置服务器' : '去任务页提交',
          page: noProfile ? 'cluster' : 'jobs', focusJobDir: noProfile ? '' : createdDirs[0],
        });
        return;
      }
      if (model.projectKind === 'molecule_library') {
        VCS.nextStep({
          title: '锂硫 / 分子参考库已就绪',
          message: `已采用 ${done} 个通过收敛与 E0 门控的物种参考态。`,
          detail: '该目录已设为默认分子参考库。下一步导入吸附构型，为每个构型选择对应物种即可生成 ΔG。',
          primaryLabel: '继续导入吸附能数据', page: 'project',
          focusSelector: '#ads-workbench',
        });
        return;
      }
      VCS.nextStep({
        title: '已导入算好的吸附能数据',
        message: `已采用 ${done} 个通过收敛与 E0 门控的结果。`,
        detail: '下一步：先查看 ΔE 汇总；锂硫参考态齐全时还可生成 ΔG 台阶图和完整报告。',
        primaryLabel: '查看 ΔE 与报告',
        onPrimary: async () => {
          await showProjectPage('#pj-delta');
          if (window.Project && typeof window.Project.selectByPath === 'function') {
            await window.Project.selectByPath(result.project_path || result.destination);
          }
          const button = $('pj-delta');
          if (button) button.click();
        },
      });
    }

    async function advance() {
      if (model.phase === 'map') await runPreview();
      else await commit();
    }

    renderMap();
    return modal;
  }

  async function openImport(mode) {
    await showProjectPage();
    const picked = await VCS.call('pick_dir');
    if (picked && picked.error) { VCS.toast('选择文件夹失败：' + picked.error, 'fail'); return; }
    if (!picked || !picked.path) return;
    VCS.toast('正在递归扫描 VASP 计算目录…');
    VCS.log((mode === 'molecules' ? '扫描分子参考库：' : '扫描吸附能文件夹：') + picked.path);
    const [scan, profileResult] = await Promise.all([
      VCS.call('proj_import_scan', picked.path),
      VCS.call('list_profiles'),
    ]);
    if (!scan || scan.ok === false || scan.error) {
      VCS.modal({ title: '文件夹扫描失败', bodyHTML:
        `<div class="ri-alert fail">${VCS.esc(scan && scan.error || '未知错误')}</div>` });
      return;
    }
    const candidates = scan.preview || scan.candidates || [];
    if (!candidates.length) {
      VCS.modal({ title: '没有发现 VASP 计算目录', bodyHTML:
        '<p>请选择包含 INCAR / POSCAR / KPOINTS / POTCAR，或 OUTCAR / OSZICAR 的根文件夹。软件会递归查找其子目录。</p>' });
      return;
    }
    const wizardMode = mode === 'molecules' ? 'molecules' : (mode === 'inputs' ? 'inputs' : 'results');
    openWizard(scan, wizardMode,
      (profileResult && profileResult.profiles) || []);
  }

  async function openNewProject() {
    await showProjectPage('#pj-create-card');
    const card = $('pj-create-card');
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    const name = $('pj-name');
    if (name) name.focus();
  }

  function init() {
    const results = $('ads-import-results');
    const inputs = $('ads-import-inputs');
    const molecules = $('ads-import-molecules');
    const create = $('ads-new-from-structure');
    if (results) results.addEventListener('click', () => openImport('results'));
    if (inputs) inputs.addEventListener('click', () => openImport('inputs'));
    if (molecules) molecules.addEventListener('click', () => openImport('molecules'));
    if (create) create.addEventListener('click', openNewProject);
    window.Project = window.Project || {};
    window.Project.openImport = openImport;
    window.AdsorptionWorkbench = { openImport, openNewProject };
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
