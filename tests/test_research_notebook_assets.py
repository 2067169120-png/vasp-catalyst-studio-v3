"""Static and executable browser regressions for Research Notebook MVP."""
from __future__ import annotations

import json
import re

from tests.test_workspace_runtime_assets import ASSETS, _run_node


def test_notebook_is_available_in_project_and_publish_with_accessible_bilingual_controls():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    css = (ASSETS / "research-notebook.css").read_text(encoding="utf-8")
    js = (ASSETS / "research-notebook.js").read_text(encoding="utf-8")

    assert 'href="research-notebook.css"' in html
    assert 'src="research-notebook.js"' in html
    assert 'id="rn-project-panel"' in html
    assert 'id="rn-publish-panel"' in html
    assert 'data-surface="project"' in html
    assert 'data-surface="publish"' in html
    assert "研究笔记 / Research Notebook" in html
    assert "审阅待办 / Review todo" in html
    assert "Claims 与 final" in html
    assert "ValidationResult" in html
    assert "human_scientific_reviewed" in html
    assert 'data-rn="review-attestation"' in html
    assert 'aria-live="polite"' in html
    assert "@media(max-width:620px)" in css
    assert "grid-template-columns:1fr" in css
    assert "overflow-wrap:anywhere" in css
    assert "localStorage.setItem" not in js
    assert "localStorage.getItem" not in js
    assert "research_notebook_append" in js
    assert "research_notebook_tombstone" in js
    assert "research_notebook_pick_attachments" in js


def test_notebook_merge_keeps_unique_dom_assets_and_bilingual_locale_parity():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    identifiers = re.findall(r'\bid="([^"]+)"', html)
    assert len(identifiers) == len(set(identifiers))

    styles = (
        "research-recipes.css", "analysis-workbench.css", "reference-browser.css",
        "research-explorer.css", "report-workbench.css", "trajectory-player.css",
        "research-notebook.css",
    )
    scripts = (
        "structure.js", "methods.js", "research-recipes.js", "research-explorer.js",
        "analysis-workbench.js", "reference-browser.js", "report-workbench.js",
        "trajectory-player.js", "research-notebook.js",
    )
    for name in styles:
        assert html.count(f'href="{name}"') == 1, name
    for name in scripts:
        assert html.count(f'src="{name}"') == 1, name
    assert 'id="source-gateway-title"' in html

    locale_root = ASSETS.parents[1] / "shared" / "locales"

    def load_without_duplicate_keys(name: str) -> dict:
        def reject_duplicates(pairs):
            value = {}
            for key, child in pairs:
                assert key not in value, f"duplicate locale key: {key}"
                value[key] = child
            return value

        return json.loads(
            (locale_root / name).read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )

    english = load_without_duplicate_keys("en.json")
    chinese = load_without_duplicate_keys("zh.json")
    assert set(english) == set(chinese)
    for prefix in (
        "research.", "trajectory.", "analysis.authoring.", "reference.",
        "structure.", "report.", "research_recipes.", "research_notebook.",
    ):
        assert any(key.startswith(prefix) for key in english), prefix


def test_malicious_note_actor_and_review_are_rendered_only_as_text_nodes():
    _run_node(
        r"""
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
const timeline = element('rn-timeline');
const parts = new Map([
  ['timeline', timeline], ['filter-type', element('filter-type')],
  ['filter-category', element('filter-category')],
  ['filter-status', element('filter-status')], ['filter-search', element('filter-search')],
]);
root.querySelector = selector => {
  const match = selector.match(/^\[data-rn="([^"]+)"\]$/); return match ? parts.get(match[1]) || null : null;
};
root.querySelectorAll = () => [];
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
const attack = '<img src=x onerror="global.pwned=1"><script>global.pwned=2</script>';
seam.State.view = {
  integrity_status: 'current', records: [{
    record_id: 'rn-malicious', revision: 1, record_type: 'review', category: 'review',
    active: true, created_at_utc: '2026-08-15T00:00:00+00:00', body: attack,
    record_digest: 'a'.repeat(64), links: [], attachments: [],
    actor: { id: 'evil', display_name: attack, role: 'reviewer',
      identity_assurance: 'self-asserted-local' },
    review: { decision: 'comment', requested_changes: [attack],
      cryptographic_signature: false },
  }], review_todo: [], limitations: [], denominator: { records: 1, active: 1, review_todo: 0 },
};
seam.renderTimeline(root);
assert.strictEqual(timeline.children.length, 1);
const card = timeline.children[0];
const all = [];
function visit(item) { all.push(item); (item.children || []).forEach(visit); }
visit(card);
assert.strictEqual(global.pwned, undefined);
assert.ok(all.some(item => item.textContent === attack), 'literal body must remain visible as text');
assert.ok(!all.some(item => ['IMG', 'SCRIPT'].includes(item.tagName)), 'injected elements must not be created');
assert.ok(all.every(item => item.innerHTML === ''), 'user content must never enter innerHTML');
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_resume_draft_marker_contains_only_opaque_reference_not_body_or_attachment():
    _run_node(
        r"""
const saves = []; const removals = [];
VCS.workspace = { drafts: {
  save(id, body, metadata) { saves.push({ id, body, metadata }); return {}; },
  remove(id) { removals.push(id); },
} };
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
root.querySelector = () => null; root.querySelectorAll = () => [];
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.State.projectId = 'project-' + 'a'.repeat(32);
root.__secretBody = 'password=must-never-enter-storage';
root.__attachmentPath = 'C:\\private\\evidence.pdf';
assert.strictEqual(seam.persistDraftReference(root), true);
assert.strictEqual(saves.length, 1);
assert.strictEqual(saves[0].id, 'research-notebook-project-' + 'a'.repeat(32));
assert.deepStrictEqual(JSON.parse(saves[0].body), {
  schema: 'vcstudio.safe-draft-ref/v1', kind: 'research-notebook',
  project_id: 'project-' + 'a'.repeat(32),
});
const encoded = JSON.stringify(saves[0]);
assert.ok(!encoded.includes('must-never-enter-storage'));
assert.ok(!encoded.includes('private\\evidence'));
seam.clearDraftReference();
assert.deepStrictEqual(removals, ['research-notebook-project-' + 'a'.repeat(32)]);
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_frontend_binds_append_to_current_revision_and_cannot_submit_gate_fields():
    _run_node(
        r"""
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
const parts = new Map();
function add(name, props = {}) { const value = element('rn-' + name, props); parts.set(name, value); return value; }
add('record-type', { value: 'note' }); add('category', { value: 'observation' });
add('body', { value: 'Measured observation' }); add('actor-id', { value: 'alice' });
add('actor-name', { value: 'Alice' }); add('actor-role', { value: 'researcher' });
add('status'); add('summary'); add('timeline'); add('todo'); add('limitations');
add('attachment-summary'); add('staged-links');
root.querySelector = selector => {
  const match = selector.match(/^\[data-rn="([^"]+)"\]$/); return match ? parts.get(match[1]) || null : null;
};
root.querySelectorAll = () => [];
const calls = [];
VCS.workspace = { drafts: { remove() {} } };
VCS.call = async (...args) => {
  calls.push(args);
  const recordId = 'rn-' + 'd'.repeat(32); const recordDigest = 'e'.repeat(64);
  return { schema: 'vcstudio.research-notebook-public/v1', ok: true,
    project_id: 'project-' + 'a'.repeat(32), revision: 8, integrity_status: 'current',
    head_digest: 'b'.repeat(64), project_identity_digest: 'c'.repeat(64),
    records: [{ record_id: recordId, revision: 8, record_digest: recordDigest }],
    active_records: [], review_todo: [], limitations: [],
    denominator: { records: 1, active: 1, review_todo: 0 },
    created_record_id: recordId,
    operation_receipt: { idempotency_key: args[4], request_digest: 'f'.repeat(64),
      revision: 8, record_id: recordId, record_digest: recordDigest, replayed: false } };
};
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.State.projectId = 'project-' + 'a'.repeat(32);
seam.State.view = { revision: 7, head_digest: 'a'.repeat(64),
  project_identity_digest: 'c'.repeat(64), records: [], review_todo: [], limitations: [],
  denominator: { records: 0, active: 0, review_todo: 0 }, integrity_status: 'current' };
assert.strictEqual(await seam.saveEntry(root, false), true);
assert.strictEqual(calls.length, 1);
assert.strictEqual(calls[0][0], 'research_notebook_append');
assert.deepStrictEqual(calls[0][3], {
  revision: 7, head_digest: 'a'.repeat(64), project_identity_digest: 'c'.repeat(64),
});
assert.match(calls[0][4], /^notebook-operation\.[A-Za-z0-9_.:-]+$/);
const request = calls[0][2];
assert.deepStrictEqual(request.actor, { id: 'alice', display_name: 'Alice', role: 'researcher' });
for (const forbidden of ['scientific_qualification', 'human_scientific_reviewed',
  'reviewer_type', 'cryptographic_signature']) {
  assert.ok(!Object.prototype.hasOwnProperty.call(request, forbidden));
}
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_save_picker_and_tombstone_ignore_stale_project_responses_last_wins():
    _run_node(
        r"""
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
const parts = new Map();
function add(name, props = {}) { const value = element('rn-' + name, props); parts.set(name, value); return value; }
add('record-type', { value: 'note' }); add('category', { value: 'observation' });
const body = add('body', { value: 'must remain' }); add('actor-id', { value: 'alice' });
add('actor-name', { value: 'Alice' }); add('actor-role', { value: 'PI' });
add('status'); add('summary'); add('timeline'); add('todo'); add('limitations');
add('attachment-summary'); add('staged-links');
root.querySelector = selector => {
  const match = selector.match(/^\[data-rn="([^"]+)"\]$/); return match ? parts.get(match[1]) || null : null;
};
root.querySelectorAll = () => [];
const A = 'project-' + 'a'.repeat(32); const B = 'project-' + 'b'.repeat(32);
const identityA = 'c'.repeat(64); const identityB = 'd'.repeat(64);
function view(projectId, revision, head, identity) {
  return { schema: 'vcstudio.research-notebook-public/v1', ok: true, project_id: projectId,
    revision, head_digest: head, project_identity_digest: identity,
    integrity_status: 'current', records: [], active_records: [], review_todo: [],
    limitations: [], denominator: { records: 0, active: 0, review_todo: 0 } };
}
let resolveCall = null; const calls = [];
VCS.workspace = { state: { project_id: A }, drafts: { remove() {}, save() {} } };
VCS.call = (...args) => { calls.push(args); return new Promise(resolve => { resolveCall = resolve; }); };
VCS.confirm = async () => true;
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
function setA(generation) {
  seam.State.generation = generation; seam.State.projectId = A;
  seam.State.view = view(A, 1, '1'.repeat(64), identityA);
  seam.State.busy = false; VCS.workspace.state.project_id = A;
}
function switchToB(generation) {
  seam.State.generation = generation; seam.State.projectId = B;
  seam.State.view = view(B, 9, '9'.repeat(64), identityB);
  seam.State.busy = false; VCS.workspace.state.project_id = B;
}

setA(1);
const savePending = seam.saveEntry(root, false);
assert.deepStrictEqual(calls[0][3], {
  revision: 1, head_digest: '1'.repeat(64), project_identity_digest: identityA });
switchToB(2);
resolveCall(view(A, 2, '2'.repeat(64), identityA));
assert.strictEqual(await savePending, false);
assert.strictEqual(seam.State.projectId, B); assert.strictEqual(seam.State.view.revision, 9);
assert.strictEqual(body.value, 'must remain');

setA(3); root.__rnAttachmentToken = '';
const pickPending = seam.pickAttachments(root);
assert.deepStrictEqual(calls[1][2], {
  revision: 1, head_digest: '1'.repeat(64), project_identity_digest: identityA });
switchToB(4);
resolveCall({ ok: true, project_id: A, revision: 1, head_digest: '1'.repeat(64),
  project_identity_digest: identityA, cancelled: false,
  selection_token: 'notebook-attachment.stale', files: [{ name: 'x', size: 1 }] });
assert.strictEqual(await pickPending, false);
assert.strictEqual(root.__rnAttachmentToken, ''); assert.strictEqual(seam.State.projectId, B);

setA(5);
const tombstonePending = seam.tombstoneRecord(root, 'rn-old');
await Promise.resolve();
assert.deepStrictEqual(calls[2][5], {
  revision: 1, head_digest: '1'.repeat(64), project_identity_digest: identityA });
assert.match(calls[2][6], /^notebook-operation\.[A-Za-z0-9_.:-]+$/);
switchToB(6);
resolveCall(view(A, 2, '2'.repeat(64), identityA));
assert.strictEqual(await tombstonePending, false);
assert.strictEqual(seam.State.projectId, B); assert.strictEqual(seam.State.view.revision, 9);
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_append_transport_retry_reuses_key_accepts_replay_receipt_and_stores_no_draft_data():
    _run_node(
        r"""
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
const parts = new Map();
function add(name, props = {}) { const value = element('rn-' + name, props); parts.set(name, value); return value; }
add('record-type', { value: 'note' }); add('category', { value: 'observation' });
const body = add('body', { value: 'sensitive retry body C:\\private\\note.txt' });
add('actor-id', { value: 'alice' }); add('actor-name', { value: 'Alice' });
add('actor-role', { value: 'researcher' }); add('status'); add('summary');
add('timeline'); add('todo'); add('limitations'); add('attachment-summary'); add('staged-links');
root.querySelector = selector => {
  const match = selector.match(/^\[data-rn="([^"]+)"\]$/); return match ? parts.get(match[1]) || null : null;
};
root.querySelectorAll = () => [];
const projectId = 'project-' + 'a'.repeat(32); const identity = 'b'.repeat(64);
VCS.workspace = { state: { project_id: projectId }, drafts: { remove() {} } };
const calls = []; let attempt = 0;
VCS.call = async (...args) => {
  calls.push(args); attempt += 1;
  if (attempt === 1) throw new Error('transport outcome unknown');
  const recordId = 'rn-' + 'c'.repeat(32); const recordDigest = 'd'.repeat(64);
  return { schema: 'vcstudio.research-notebook-public/v1', ok: true,
    project_id: projectId, project_identity_digest: identity, revision: 2,
    head_digest: recordDigest, integrity_status: 'current', created_record_id: recordId,
    records: [{ record_id: recordId, revision: 2, record_digest: recordDigest,
      record_type: 'note', category: 'observation', active: true, body: body.value,
      created_at_utc: '2026-08-21T00:00:00+00:00', actor: { id: 'alice', display_name: 'Alice', role: 'researcher' },
      links: [], attachments: [] }], active_records: [], review_todo: [], limitations: [],
    denominator: { records: 1, active: 1, review_todo: 0 },
    operation_receipt: { idempotency_key: args[4], request_digest: 'e'.repeat(64),
      revision: 2, record_id: recordId, record_digest: recordDigest, replayed: true } };
};
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.State.projectId = projectId; seam.State.generation = 1;
seam.State.view = { project_id: projectId, project_identity_digest: identity,
  revision: 1, head_digest: 'a'.repeat(64), integrity_status: 'current',
  records: [], review_todo: [], limitations: [], denominator: {} };
assert.strictEqual(await seam.saveEntry(root, false), false);
assert.strictEqual(calls.length, 1);
const firstKey = calls[0][4];
assert.match(firstKey, /^notebook-operation\.[A-Za-z0-9_.:-]+$/);
const operationRef = JSON.stringify(root.__rnOperation);
assert.ok(!operationRef.includes('sensitive retry body'));
assert.ok(!operationRef.includes('private'));
assert.ok(!operationRef.includes('attachment'));
assert.strictEqual(await seam.saveEntry(root, false), true);
assert.strictEqual(calls[1][4], firstKey);
assert.strictEqual(body.value, '');
assert.strictEqual(root.__rnOperation, undefined);
assert.strictEqual(seam.State.view.operation_receipt.replayed, true);
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_revision_conflict_refresh_reuses_semantic_operation_key_and_changed_draft_rotates_it():
    _run_node(
        r"""
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
const parts = new Map();
function add(name, props = {}) { const value = element('rn-' + name, props); parts.set(name, value); return value; }
add('record-type', { value: 'note' }); add('category', { value: 'observation' });
const body = add('body', { value: 'same semantic request' }); add('actor-id', { value: 'alice' });
add('actor-name', { value: 'Alice' }); add('actor-role', { value: 'researcher' });
add('status'); add('summary'); add('timeline'); add('todo'); add('limitations');
add('attachment-summary'); add('staged-links');
root.querySelector = selector => {
  const match = selector.match(/^\[data-rn="([^"]+)"\]$/); return match ? parts.get(match[1]) || null : null;
};
root.querySelectorAll = () => [];
const projectId = 'project-' + 'a'.repeat(32); const identity = 'b'.repeat(64);
function view(revision, head) { return { schema: 'vcstudio.research-notebook-public/v1', ok: true,
  project_id: projectId, project_identity_digest: identity, revision, head_digest: head,
  integrity_status: 'current', records: [], active_records: [], review_todo: [],
  limitations: [], denominator: { records: 0, active: 0, review_todo: 0 } }; }
VCS.workspace = { state: { project_id: projectId }, drafts: { remove() {} } };
const appendCalls = []; let appendCount = 0;
VCS.call = async (...args) => {
  if (args[0] === 'research_notebook_bootstrap') return view(2, '2'.repeat(64));
  appendCalls.push(args); appendCount += 1;
  if (appendCount === 1) return { ok: false, error_code: 'revision_conflict',
    error: 'conflict', revision: 2, head_digest: '2'.repeat(64) };
  if (appendCount === 2) throw new Error('changed draft stopped before server commit');
  throw new Error('unexpected append');
};
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.State.projectId = projectId; seam.State.generation = 1;
seam.State.view = view(1, '1'.repeat(64));
assert.strictEqual(await seam.saveEntry(root, false), false);
const conflictKey = appendCalls[0][4];
await waitFor(() => !seam.State.busy && seam.State.view && seam.State.view.revision === 2,
  'conflict refresh did not settle');
assert.strictEqual(root.__rnOperation.idempotencyKey, conflictKey);
body.value = 'changed semantic request';
assert.strictEqual(await seam.saveEntry(root, false), false);
assert.notStrictEqual(appendCalls[1][4], conflictKey);
assert.ok(!JSON.stringify(root.__rnOperation).includes('changed semantic request'));
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_success_without_a_bound_operation_receipt_is_rejected_fail_closed():
    _run_node(
        r"""
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
const parts = new Map();
function add(name, props = {}) { const value = element('rn-' + name, props); parts.set(name, value); return value; }
add('record-type', { value: 'note' }); add('category', { value: 'observation' });
const body = add('body', { value: 'must remain after forged success' });
add('actor-id', { value: 'alice' }); add('actor-name', { value: 'Alice' });
add('actor-role', { value: 'researcher' }); add('status'); add('summary');
add('timeline'); add('todo'); add('limitations'); add('attachment-summary'); add('staged-links');
root.querySelector = selector => {
  const match = selector.match(/^\[data-rn="([^"]+)"\]$/); return match ? parts.get(match[1]) || null : null;
};
root.querySelectorAll = () => [];
const projectId = 'project-' + 'a'.repeat(32); const identity = 'b'.repeat(64);
VCS.workspace = { state: { project_id: projectId }, drafts: { remove() {} } };
VCS.call = async () => ({ ok: true, project_id: projectId,
  project_identity_digest: identity, revision: 2, head_digest: 'c'.repeat(64),
  integrity_status: 'current', records: [], review_todo: [], limitations: [], denominator: {} });
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.State.projectId = projectId; seam.State.generation = 1;
seam.State.view = { project_id: projectId, project_identity_digest: identity,
  revision: 1, head_digest: 'a'.repeat(64), integrity_status: 'current',
  records: [], review_todo: [], limitations: [], denominator: {} };
assert.strictEqual(await seam.saveEntry(root, false), false);
assert.strictEqual(body.value, 'must remain after forged success');
assert.ok(root.__rnOperation, 'unknown outcome must retain the opaque retry reference');
assert.strictEqual(seam.State.view.revision, 1);
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_link_jump_uses_safe_semantic_route_not_a_filesystem_locator():
    _run_node(
        r"""
const navigations = [];
VCS.workspace = { navigateRoute(id, options) { navigations.push({ id, options }); } };
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.State.projectId = 'project-' + 'a'.repeat(32);
seam.State.view = { records: [{ record_id: 'rn-1', links: [{
  kind: 'report_revision', id: 'report-r0001', status: 'current',
  route: { id: 'publish-versions', project_id: 'project-' + 'a'.repeat(32),
    revision_id: 'report-r0001' },
}] }] };
assert.strictEqual(seam.jumpLink('rn-1', 0), true);
assert.deepStrictEqual(navigations, [{ id: 'publish-versions', options: {
  projectId: 'project-' + 'a'.repeat(32), query: { revision: 'report-r0001' },
  source: 'research-notebook-link',
} }]);
assert.ok(!JSON.stringify(navigations).includes('C:\\'));
""",
        str(ASSETS / "research-notebook.js"),
    )


def test_ctrl_enter_and_escape_keyboard_paths_are_wired_without_mouse_dependency():
    _run_node(
        r"""
const root = element('rn-project-panel', { dataset: { surface: 'project' } });
const linkId = element('rn-link-id', { value: 'job-a' });
const linkKind = element('rn-link-kind', { value: 'job' });
const linkRevision = element('rn-link-revision', { value: '' });
const staged = element('rn-staged');
const body = element('rn-body', { value: 'draft' });
const parts = new Map([['link-id', linkId], ['link-kind', linkKind],
  ['link-revision', linkRevision], ['staged-links', staged], ['body', body]]);
root.querySelector = selector => {
  const match = selector.match(/^\[data-rn="([^"]+)"\]$/); return match ? parts.get(match[1]) || null : null;
};
root.querySelectorAll = () => [];
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.wireRoot(root);
let prevented = false;
root.dispatchEvent({ type: 'keydown', key: 'Enter', target: linkId,
  preventDefault() { prevented = true; } });
assert.strictEqual(prevented, true);
assert.deepStrictEqual(root.__rnLinks, [{ kind: 'job', id: 'job-a', report_revision_id: null }]);
root.__rnEditingId = 'rn-old';
root.dispatchEvent({ type: 'keydown', key: 'Escape', target: body,
  preventDefault() {} });
assert.strictEqual(root.__rnEditingId, '');
""",
        str(ASSETS / "research-notebook.js"),
    )
