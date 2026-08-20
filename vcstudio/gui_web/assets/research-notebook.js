(function () {
  'use strict';

  const PROJECT_ID_RE = /^(?:project-[a-f0-9]{32}|registry-[a-f0-9]{24})$/;
  const State = {
    projectId: '', view: null, busy: false, generation: 0,
  };

  const COPY = {
    'research_notebook.runtime.loading': ['正在读取项目 journal…', 'Loading the project journal…'],
    'research_notebook.runtime.empty': ['尚无记录。问题、假设、观察、解释、限制和下一步都可从这里开始。',
      'No records yet. Start with a problem, hypothesis, observation, interpretation, limitation, or next step.'],
    'research_notebook.runtime.no_project': ['请先选择一个已登记项目。', 'Select a registered project first.'],
    'research_notebook.runtime.saved': ['已写入不可变 journal revision。', 'Saved as a new immutable journal revision.'],
    'research_notebook.runtime.conflict': ['journal 已被另一操作更新，已刷新；请核对后重试。',
      'The journal changed in another operation. It was refreshed; review and retry.'],
    'research_notebook.runtime.tampered': ['journal 完整性校验失败；记录已 fail-closed 隐藏，请检查项目文件。',
      'Journal integrity verification failed. Records are hidden fail-closed; inspect the project files.'],
    'research_notebook.runtime.current': ['当前', 'Current'],
    'research_notebook.runtime.stale': ['已过期', 'Stale'],
    'research_notebook.runtime.missing': ['缺失', 'Missing'],
    'research_notebook.runtime.unknown': ['未知', 'Unknown'],
    'research_notebook.runtime.edit': ['编辑（追加新版本）', 'Edit (append revision)'],
    'research_notebook.runtime.tombstone': ['删除（追加 tombstone）', 'Delete (append tombstone)'],
    'research_notebook.runtime.cite': ['复制引用 ID', 'Copy citation ID'],
    'research_notebook.runtime.jump': ['跳转', 'Open'],
    'research_notebook.runtime.copied': ['已复制 notebook 引用；它不会自动变成 report claim。',
      'Notebook citation copied. It does not automatically become a report claim.'],
    'research_notebook.runtime.confirm_delete': ['将追加 tombstone；历史记录仍保留。继续吗？',
      'A tombstone will be appended; history remains. Continue?'],
    'research_notebook.runtime.deleted': ['已追加 tombstone。', 'Tombstone appended.'],
    'research_notebook.runtime.attachment_ready': ['附件已在服务端暂存选择；正文和文件字节未写入 localStorage。',
      'Attachment selection is held server-side; body and file bytes were not written to localStorage.'],
    'research_notebook.runtime.local_actor': ['本地自声明 actor；不是登录认证或密码学签名。',
      'Locally self-attributed actor; not login authentication or a cryptographic signature.'],
    'research_notebook.todo.empty': ['待处理 0', '0 pending'],
  };

  function tr(key) {
    const pair = COPY[key] || [key, key];
    const fallback = String(VCS.i18n && VCS.i18n.lang || '').toLowerCase().startsWith('en')
      ? pair[1] : pair[0];
    return typeof VCS.t === 'function' ? VCS.t(key, {}, fallback) : fallback;
  }

  function safeId(value) {
    const text = String(value || '').trim().toLowerCase();
    return PROJECT_ID_RE.test(text) ? text : '';
  }

  function currentProjectId() {
    if (window.Project && typeof window.Project.current === 'function') {
      const project = window.Project.current();
      const id = safeId(project && (project.project_id || project.id));
      if (id) return id;
    }
    const workspace = VCS.workspace || {};
    return safeId(workspace.state && workspace.state.project_id);
  }

  function mutationGuard() {
    if (!State.view || !State.projectId) return null;
    return {
      generation: State.generation,
      projectId: State.projectId,
      revision: Number(State.view.revision || 0),
      headDigest: State.view.head_digest == null ? null : String(State.view.head_digest),
      projectIdentityDigest: String(State.view.project_identity_digest || ''),
    };
  }

  function guardProjectCurrent(guard) {
    if (!guard || guard.generation !== State.generation
        || guard.projectId !== State.projectId) return false;
    const selected = currentProjectId();
    return !selected || selected === guard.projectId;
  }

  function guardCurrent(guard) {
    if (!guardProjectCurrent(guard) || !State.view
        || Number(State.view.revision || 0) !== guard.revision
        || (State.view.head_digest == null ? null : String(State.view.head_digest)) !== guard.headDigest
        || String(State.view.project_identity_digest || '') !== guard.projectIdentityDigest) return false;
    return true;
  }

  function expectedGuard(guard) {
    return {
      revision: guard.revision, head_digest: guard.headDigest,
      project_identity_digest: guard.projectIdentityDigest,
    };
  }

  function roots() {
    return ['rn-project-panel', 'rn-publish-panel']
      .map(id => document.getElementById(id)).filter(Boolean);
  }

  function part(root, name) {
    return root && root.querySelector ? root.querySelector(`[data-rn="${name}"]`) : null;
  }

  function textPart(root, name, value) {
    const target = part(root, name);
    if (target) target.textContent = String(value == null ? '' : value);
  }

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }

  function draftKey(projectId = State.projectId) {
    return projectId ? `research-notebook-${projectId}` : '';
  }

  function persistDraftReference(root) {
    const key = draftKey();
    const drafts = VCS.workspace && VCS.workspace.drafts;
    if (!key || !(drafts && typeof drafts.save === 'function')) return false;
    const marker = JSON.stringify({
      schema: 'vcstudio.safe-draft-ref/v1', kind: 'research-notebook',
      project_id: State.projectId,
    });
    drafts.save(key, marker, {
      label: 'Research Notebook', surface: String(root && root.dataset.surface || 'project'),
    });
    return true;
  }

  function clearDraftReference() {
    const key = draftKey();
    const drafts = VCS.workspace && VCS.workspace.drafts;
    if (key && drafts && typeof drafts.remove === 'function') drafts.remove(key);
  }

  function setStatus(root, message, kind = '') {
    const target = part(root, 'status');
    if (!target) return;
    target.textContent = String(message || '');
    target.dataset.state = kind || 'neutral';
  }

  function setBusy(value) {
    State.busy = !!value;
    roots().forEach(root => {
      root.setAttribute('aria-busy', State.busy ? 'true' : 'false');
      root.querySelectorAll(
        '[data-rn-action], [data-rn-edit], [data-rn-tombstone], [data-rn-remove-link]'
      ).forEach(button => { button.disabled = State.busy; });
    });
  }

  function recordMatches(root, record) {
    const type = part(root, 'filter-type');
    const category = part(root, 'filter-category');
    const linkStatus = part(root, 'filter-status');
    const search = part(root, 'filter-search');
    if (type && type.value && record.record_type !== type.value) return false;
    if (category && category.value && record.category !== category.value) return false;
    if (linkStatus && linkStatus.value && !(record.links || [])
      .some(link => link.status === linkStatus.value)) return false;
    const query = String(search && search.value || '').trim().toLowerCase();
    if (!query) return true;
    return [record.body, record.record_id, record.category,
      record.actor && record.actor.display_name, record.actor && record.actor.role]
      .some(value => String(value || '').toLowerCase().includes(query));
  }

  function statusBadge(link) {
    const status = ['current', 'stale', 'missing'].includes(link.status)
      ? link.status : 'unknown';
    return `${link.kind}:${link.id} · ${tr(`research_notebook.runtime.${status}`)}`;
  }

  function renderRecord(root, record) {
    const card = node('article', `rn-record ${record.active ? 'active' : 'inactive'}`);
    card.dataset.recordId = record.record_id;
    const header = node('header', 'rn-record-head');
    const identity = node('div', 'rn-record-identity');
    identity.append(node('b', '', `${record.category} · r${record.revision}`));
    identity.append(node('small', '', `${record.record_id} · ${record.created_at_utc || ''}`));
    header.append(identity);
    const lifecycle = node('span', `rn-lifecycle ${record.active ? 'active' : 'inactive'}`,
      record.active ? tr('research_notebook.runtime.current')
        : (record.record_type === 'tombstone' ? 'tombstone' : 'superseded'));
    header.append(lifecycle);
    card.append(header);

    const body = node('pre', 'rn-record-body');
    body.textContent = String(record.body || '');
    card.append(body);
    const actor = record.actor || {};
    card.append(node('p', 'rn-actor',
      `${actor.display_name || actor.id || 'actor'} · ${actor.role || ''} · ${actor.identity_assurance || ''}`));

    if (record.review) {
      const review = node('section', 'rn-review-summary');
      review.append(node('b', '', `Human Review · ${record.review.decision}`));
      (record.review.requested_changes || []).forEach(change => {
        review.append(node('p', '', `• ${change}`));
      });
      review.append(node('small', '', record.review.cryptographic_signature === false
        ? tr('research_notebook.runtime.local_actor') : ''));
      card.append(review);
    }

    if ((record.links || []).length) {
      const links = node('div', 'rn-links');
      record.links.forEach((link, index) => {
        const button = node('button', `rn-link status-${link.status || 'unknown'}`,
          `${statusBadge(link)} · ${tr('research_notebook.runtime.jump')}`);
        button.type = 'button';
        button.dataset.rnLinkRecord = record.record_id;
        button.dataset.rnLinkIndex = String(index);
        button.disabled = !link.route;
        links.append(button);
      });
      card.append(links);
    }

    if ((record.attachments || []).length) {
      const list = node('ul', 'rn-attachments');
      record.attachments.forEach(file => {
        list.append(node('li', '', `${file.name} · ${file.size} B · ${String(file.sha256 || '').slice(0, 12)}`));
      });
      card.append(list);
    }

    const actions = node('div', 'rn-record-actions');
    const cite = node('button', 'btn quiet', tr('research_notebook.runtime.cite'));
    cite.type = 'button'; cite.dataset.rnCite = record.record_id;
    actions.append(cite);
    if (root.dataset.surface !== 'publish'
      && record.active && record.record_type !== 'tombstone') {
      const edit = node('button', 'btn quiet', tr('research_notebook.runtime.edit'));
      edit.type = 'button'; edit.dataset.rnEdit = record.record_id;
      const remove = node('button', 'btn quiet', tr('research_notebook.runtime.tombstone'));
      remove.type = 'button'; remove.dataset.rnTombstone = record.record_id;
      actions.append(edit, remove);
    }
    card.append(actions);
    return card;
  }

  function renderTimeline(root) {
    const container = part(root, 'timeline');
    if (!container) return;
    container.textContent = '';
    const view = State.view;
    if (!view || view.integrity_status === 'tampered') {
      container.append(node('p', 'rn-empty', view
        ? tr('research_notebook.runtime.tampered')
        : tr('research_notebook.runtime.no_project')));
      return;
    }
    const records = (view.records || []).filter(record => recordMatches(root, record))
      .slice().sort((left, right) => Number(right.revision) - Number(left.revision));
    if (!records.length) {
      container.append(node('p', 'rn-empty', tr('research_notebook.runtime.empty')));
      return;
    }
    records.forEach(record => container.append(renderRecord(root, record)));
  }

  function renderTodo(root) {
    const container = part(root, 'todo');
    if (!container) return;
    container.textContent = '';
    const rows = State.view && State.view.review_todo || [];
    if (!rows.length) {
      container.append(node('p', 'rn-empty', tr('research_notebook.todo.empty')));
      return;
    }
    rows.forEach(record => {
      const item = node('button', 'rn-todo-item');
      item.type = 'button'; item.dataset.rnFocusRecord = record.record_id;
      item.textContent = `${record.review.decision} · ${record.actor.display_name} · ${record.body}`;
      container.append(item);
    });
  }

  function renderLimitations(root) {
    const container = part(root, 'limitations');
    if (!container) return;
    container.textContent = '';
    (State.view && State.view.limitations || []).forEach(item => {
      container.append(node('li', '', String(VCS.i18n && VCS.i18n.lang || '').startsWith('en')
        ? item.en : item.zh));
    });
  }

  function renderSummary(root) {
    const view = State.view;
    const counts = view && view.denominator || {};
    textPart(root, 'summary', view
      ? `revision ${view.revision} · records ${counts.records || 0} · active ${counts.active || 0} · review todo ${counts.review_todo || 0}`
      : tr('research_notebook.runtime.no_project'));
    if (view && view.integrity_status === 'tampered') {
      setStatus(root, tr('research_notebook.runtime.tampered'), 'error');
    }
    renderTimeline(root); renderTodo(root); renderLimitations(root);
  }

  function renderAll() {
    roots().forEach(renderSummary);
  }

  function stagedLinks(root) {
    if (!Array.isArray(root.__rnLinks)) root.__rnLinks = [];
    return root.__rnLinks;
  }

  function renderStagedLinks(root) {
    const container = part(root, 'staged-links');
    if (!container) return;
    container.textContent = '';
    stagedLinks(root).forEach((link, index) => {
      const item = node('li', 'rn-staged-link');
      item.append(node('span', '', `${link.kind}:${link.id}${link.report_revision_id ? ` @ ${link.report_revision_id}` : ''}`));
      const remove = node('button', 'btn quiet', '×');
      remove.type = 'button'; remove.dataset.rnRemoveLink = String(index);
      item.append(remove); container.append(item);
    });
  }

  function addStagedLink(root) {
    const kind = String(part(root, 'link-kind') && part(root, 'link-kind').value || '').trim();
    const id = String(part(root, 'link-id') && part(root, 'link-id').value || '').trim();
    const revision = String(part(root, 'link-revision') && part(root, 'link-revision').value || '').trim();
    if (!kind || !id) return false;
    stagedLinks(root).push({ kind, id, report_revision_id: revision || null });
    if (part(root, 'link-id')) part(root, 'link-id').value = '';
    renderStagedLinks(root); persistDraftReference(root); return true;
  }

  function readActor(root, review = false) {
    const prefix = review ? 'reviewer-' : 'actor-';
    return {
      id: String(part(root, prefix + 'id') && part(root, prefix + 'id').value || '').trim(),
      display_name: String(part(root, prefix + 'name') && part(root, prefix + 'name').value || '').trim(),
      role: String(part(root, prefix + 'role') && part(root, prefix + 'role').value || '').trim(),
    };
  }

  function splitLines(value) {
    return String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  }

  function clearCompose(root) {
    ['body', 'review-body', 'requested-changes', 'signature-attribution'].forEach(name => {
      const input = part(root, name); if (input) input.value = '';
    });
    const attestation = part(root, 'review-attestation'); if (attestation) attestation.checked = false;
    root.__rnEditingId = ''; root.__rnLinks = []; root.__rnAttachmentToken = '';
    renderStagedLinks(root); textPart(root, 'attachment-summary', ''); clearDraftReference();
  }

  function applyView(result, root, successMessage, guard) {
    if (!guardCurrent(guard)) return false;
    if (!result || result.ok !== true) {
      if (result && result.error_code === 'revision_conflict') {
        setStatus(root, tr('research_notebook.runtime.conflict'), 'warning'); load(root); return false;
      }
      setStatus(root, String(result && result.error || 'Notebook unavailable'), 'error');
      return false;
    }
    if (safeId(result.project_id) !== guard.projectId
        || String(result.project_identity_digest || '') !== guard.projectIdentityDigest
        || Number(result.revision) !== guard.revision + 1) return false;
    State.view = result;
    clearCompose(root); renderAll(); setStatus(root, successMessage, 'success'); return true;
  }

  async function saveEntry(root, reviewMode = false) {
    if (State.busy || !State.view || !State.projectId) return false;
    const guard = mutationGuard(); if (!guard) return false;
    const recordType = reviewMode ? 'review'
      : String(part(root, 'record-type') && part(root, 'record-type').value || 'note');
    const category = reviewMode ? 'review'
      : recordType === 'decision' ? 'decision'
        : String(part(root, 'category') && part(root, 'category').value || 'observation');
    const bodyField = part(root, reviewMode ? 'review-body' : 'body');
    const request = {
      record_type: recordType, category, body: String(bodyField && bodyField.value || ''),
      actor: readActor(root, reviewMode), links: stagedLinks(root).slice(),
      supersedes: root.__rnEditingId || null,
      attachment_selection_token: root.__rnAttachmentToken || null,
    };
    if (reviewMode) {
      request.review = {
        decision: String(part(root, 'review-decision') && part(root, 'review-decision').value || ''),
        requested_changes: splitLines(part(root, 'requested-changes') && part(root, 'requested-changes').value),
        signature_attribution: String(part(root, 'signature-attribution') && part(root, 'signature-attribution').value || ''),
        local_human_attestation: !!(part(root, 'review-attestation') && part(root, 'review-attestation').checked),
      };
    }
    setBusy(true); setStatus(root, tr('research_notebook.runtime.loading'));
    try {
      const result = await VCS.call('research_notebook_append',
        guard.projectId, request, expectedGuard(guard));
      return applyView(result, root, tr('research_notebook.runtime.saved'), guard);
    } catch (error) {
      if (guardProjectCurrent(guard)) {
        setStatus(root, error && error.message || String(error), 'error');
      }
      return false;
    } finally { if (guardProjectCurrent(guard)) setBusy(false); }
  }

  async function pickAttachments(root) {
    if (!State.projectId || State.busy) return false;
    const guard = mutationGuard(); if (!guard) return false;
    setBusy(true);
    try {
      const result = await VCS.call(
        'research_notebook_pick_attachments', guard.projectId, expectedGuard(guard));
      if (!guardCurrent(guard)) return false;
      if (!result || result.ok !== true) throw new Error(result && result.error || 'Attachment selection failed');
      if (result.cancelled) return false;
      if (safeId(result.project_id) !== guard.projectId
          || Number(result.revision) !== guard.revision
          || (result.head_digest == null ? null : String(result.head_digest)) !== guard.headDigest
          || String(result.project_identity_digest || '') !== guard.projectIdentityDigest) return false;
      root.__rnAttachmentToken = String(result.selection_token || '');
      textPart(root, 'attachment-summary', (result.files || [])
        .map(file => `${file.name} · ${file.size} B`).join('；'));
      setStatus(root, tr('research_notebook.runtime.attachment_ready'), 'success');
      persistDraftReference(root); return true;
    } catch (error) {
      if (guardProjectCurrent(guard)) {
        setStatus(root, error && error.message || String(error), 'error');
      }
      return false;
    } finally { if (guardProjectCurrent(guard)) setBusy(false); }
  }

  function findRecord(recordId) {
    return (State.view && State.view.records || [])
      .find(record => record.record_id === recordId) || null;
  }

  function editRecord(root, recordId) {
    const record = findRecord(recordId); if (!record || !record.active) return false;
    const review = record.record_type === 'review';
    const body = part(root, review ? 'review-body' : 'body'); if (body) body.value = record.body || '';
    if (!review) {
      if (part(root, 'record-type')) part(root, 'record-type').value = record.record_type;
      if (part(root, 'category')) part(root, 'category').value = record.category;
    } else {
      if (part(root, 'review-decision')) part(root, 'review-decision').value = record.review.decision;
      if (part(root, 'requested-changes')) part(root, 'requested-changes').value =
        (record.review.requested_changes || []).join('\n');
    }
    root.__rnEditingId = record.record_id;
    root.__rnLinks = (record.links || []).map(link => ({
      kind: link.kind, id: link.id, report_revision_id: link.report_revision_id || null,
    }));
    renderStagedLinks(root); persistDraftReference(root);
    if (body && body.focus) body.focus(); return true;
  }

  async function tombstoneRecord(root, recordId) {
    if (!State.view || State.busy) return false;
    const guard = mutationGuard(); if (!guard) return false;
    const confirmed = typeof VCS.confirm === 'function'
      ? await VCS.confirm(tr('research_notebook.runtime.confirm_delete'))
        : window.confirm(tr('research_notebook.runtime.confirm_delete'));
    if (!confirmed) return false;
    if (!guardCurrent(guard)) return false;
    const reason = 'Removed from active notebook by explicit local action.';
    const actor = readActor(root, false);
    setBusy(true);
    try {
      const result = await VCS.call('research_notebook_tombstone',
        guard.projectId, recordId, reason, actor, expectedGuard(guard));
      return applyView(result, root, tr('research_notebook.runtime.deleted'), guard);
    } catch (error) {
      if (guardProjectCurrent(guard)) {
        setStatus(root, error && error.message || String(error), 'error');
      }
      return false;
    } finally { if (guardProjectCurrent(guard)) setBusy(false); }
  }

  async function citeRecord(recordId) {
    const record = findRecord(recordId); if (!record) return false;
    const citation = `notebook:${record.record_id}@${String(record.record_digest || '').slice(0, 16)}`;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(citation);
      }
    } catch (_) { /* clipboard may be unavailable in a restricted WebView */ }
    if (typeof VCS.toast === 'function') {
      VCS.toast(`${tr('research_notebook.runtime.copied')} ${citation}`);
    }
    return citation;
  }

  function jumpLink(recordId, rawIndex) {
    const record = findRecord(recordId);
    const link = record && (record.links || [])[Number(rawIndex)];
    const route = link && link.route;
    const workspace = VCS.workspace;
    if (!route || !(workspace && typeof workspace.navigateRoute === 'function')) return false;
    const query = {};
    if (route.revision_id) query.revision = route.revision_id;
    if (route.job_id) query.job = route.job_id;
    if (route.source_id) query.source = route.source_id;
    workspace.navigateRoute(route.id, {
      projectId: route.project_id || State.projectId, query, source: 'research-notebook-link',
    });
    return true;
  }

  function focusRecord(root, recordId) {
    const target = root.querySelector(`[data-record-id="${recordId}"]`);
    if (target && target.scrollIntoView) target.scrollIntoView({ block: 'center' });
    if (target && target.focus) { target.tabIndex = -1; target.focus(); }
  }

  function onRootClick(root, event) {
    const target = event.target;
    if (!target || !target.closest) return;
    const action = target.closest('[data-rn-action]');
    if (action) {
      const name = action.dataset.rnAction;
      if (name === 'refresh') load(root);
      else if (name === 'add-link') addStagedLink(root);
      else if (name === 'save') saveEntry(root, false);
      else if (name === 'review-save') saveEntry(root, true);
      else if (name === 'pick-attachments') pickAttachments(root);
      else if (name === 'cancel-edit') clearCompose(root);
      return;
    }
    const removeLink = target.closest('[data-rn-remove-link]');
    if (removeLink) {
      stagedLinks(root).splice(Number(removeLink.dataset.rnRemoveLink), 1);
      renderStagedLinks(root); return;
    }
    const edit = target.closest('[data-rn-edit]');
    if (edit) { editRecord(root, edit.dataset.rnEdit); return; }
    const remove = target.closest('[data-rn-tombstone]');
    if (remove) { tombstoneRecord(root, remove.dataset.rnTombstone); return; }
    const cite = target.closest('[data-rn-cite]');
    if (cite) { citeRecord(cite.dataset.rnCite); return; }
    const link = target.closest('[data-rn-link-record]');
    if (link) { jumpLink(link.dataset.rnLinkRecord, link.dataset.rnLinkIndex); return; }
    const focus = target.closest('[data-rn-focus-record]');
    if (focus) focusRecord(root, focus.dataset.rnFocusRecord);
  }

  function wireRoot(root) {
    if (root.dataset.rnWired === 'true') return;
    root.dataset.rnWired = 'true';
    root.addEventListener('click', event => onRootClick(root, event));
    root.addEventListener('input', event => {
      const target = event.target;
      if (target && target.matches && target.matches('[data-rn-draft-body]')) {
        persistDraftReference(root);
      }
      if (target && target.matches && target.matches('[data-rn-filter]')) renderTimeline(root);
    });
    root.addEventListener('change', event => {
      const target = event.target;
      if (target && target.matches && target.matches('[data-rn-filter]')) renderTimeline(root);
    });
    root.addEventListener('keydown', event => {
      if (event.key === 'Enter' && event.target === part(root, 'link-id')) {
        event.preventDefault(); addStagedLink(root); return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        const review = event.target && event.target.closest
          && event.target.closest('[data-rn-review-form]');
        saveEntry(root, !!review); return;
      }
      if (event.key === 'Escape' && root.__rnEditingId) {
        event.preventDefault(); clearCompose(root);
      }
    });
  }

  async function load(root) {
    const projectId = currentProjectId();
    if (!projectId) {
      State.generation += 1;
      State.projectId = ''; State.view = null; renderAll();
      roots().forEach(item => setStatus(
        item, tr('research_notebook.runtime.no_project'))); return false;
    }
    const generation = ++State.generation;
    State.projectId = projectId; setBusy(true);
    setStatus(root, tr('research_notebook.runtime.loading'));
    try {
      const result = await VCS.call('research_notebook_bootstrap', projectId);
      if (generation !== State.generation || currentProjectId() !== projectId) return false;
      if (result && result.ok === true && safeId(result.project_id) !== projectId) return false;
      State.view = result; renderAll();
      roots().forEach(item => setStatus(item,
        result && result.ok === true ? `revision ${result.revision}`
          : String(result && result.error || tr('research_notebook.runtime.tampered')),
        result && result.ok === true ? 'success' : 'error'));
      return !!(result && result.ok === true);
    } catch (error) {
      if (generation === State.generation && State.projectId === projectId) {
        setStatus(root, error && error.message || String(error), 'error');
      }
      return false;
    } finally {
      if (generation === State.generation && State.projectId === projectId) setBusy(false);
    }
  }

  function init() {
    roots().forEach(wireRoot);
    document.addEventListener('vcs:page', event => {
      const page = event.detail && event.detail.page;
      if (page === 'project' || page === 'report-workbench') {
        const root = document.getElementById(page === 'project'
          ? 'rn-project-panel' : 'rn-publish-panel');
        if (root) load(root);
      }
    });
    document.addEventListener('vcs:workspace-project', () => {
      const visible = roots().find(root => !root.closest('section.page')
        || !root.closest('section.page').hidden);
      if (visible) load(visible);
    });
    document.addEventListener('vcs:language', renderAll);
    const visible = roots().find(root => {
      const page = root.closest && root.closest('section.page');
      return !page || !page.hidden;
    });
    if (visible) load(visible);
  }

  window.ResearchNotebook = { load };
  if (window.__VCS_TEST__) {
    window.ResearchNotebook.__test = {
      State, tr, draftKey, persistDraftReference, clearDraftReference,
      renderRecord, renderTimeline, renderTodo, renderLimitations,
      addStagedLink, saveEntry, editRecord, jumpLink, citeRecord, wireRoot,
      applyView, load, mutationGuard, guardProjectCurrent, guardCurrent,
      expectedGuard, pickAttachments,
      tombstoneRecord,
    };
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
