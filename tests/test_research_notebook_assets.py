"""Static and executable browser regressions for Research Notebook MVP."""
from __future__ import annotations

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
  return { schema: 'vcstudio.research-notebook-public/v1', ok: true,
    project_id: 'project-' + 'a'.repeat(32), revision: 8, integrity_status: 'current',
    records: [], active_records: [], review_todo: [], limitations: [],
    denominator: { records: 0, active: 0, review_todo: 0 } };
};
loadAsset(process.argv[1]);
const seam = window.ResearchNotebook.__test;
seam.State.projectId = 'project-' + 'a'.repeat(32);
seam.State.view = { revision: 7, records: [], review_todo: [], limitations: [],
  denominator: { records: 0, active: 0, review_todo: 0 }, integrity_status: 'current' };
assert.strictEqual(await seam.saveEntry(root, false), true);
assert.strictEqual(calls.length, 1);
assert.strictEqual(calls[0][0], 'research_notebook_append');
assert.strictEqual(calls[0][3], 7);
const request = calls[0][2];
assert.deepStrictEqual(request.actor, { id: 'alice', display_name: 'Alice', role: 'researcher' });
for (const forbidden of ['scientific_qualification', 'human_scientific_reviewed',
  'reviewer_type', 'cryptographic_signature']) {
  assert.ok(!Object.prototype.hasOwnProperty.call(request, forbidden));
}
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
