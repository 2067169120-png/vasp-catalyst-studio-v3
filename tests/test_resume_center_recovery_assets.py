"""Executable regressions for safe import/cluster Resume Center entries."""
from __future__ import annotations

from tests.test_workspace_runtime_assets import ASSETS, _run_node


def test_project_import_draft_is_an_opaque_marker_and_cancel_clears_it():
    _run_node(
        r"""
const records = new Map();
const saves = []; const removals = [];
VCS.workspace = {
  drafts: {
    load(id) { return records.get(id) || null; },
    save(id, body, metadata) {
      const record = { schema: 'vcstudio.unverified-draft/v1', id, text: body, metadata };
      records.set(id, record); saves.push({ id, body, metadata }); return record;
    },
    remove(id) { records.delete(id); removals.push(id); },
  },
};
element('pj-import-source', { value: 'C:\\secret\\vasp-results' });
element('pj-import-name', { value: 'confidential-catalyst' });
element('pj-import-root', { value: 'D:\\private\\projects' });
element('pj-import-search', { value: 'private-row-name' });
element('pj-import-filter', { value: 'attention' });
element('pj-import-review'); element('pj-import-done');
element('pj-import-cancel', { disabled: true });
loadAsset(process.argv[1]);
const seam = window.Project && window.Project.__test;
assert.ok(seam, 'project resume test seam must be exposed');
seam.State.importResult = { candidates: [{ path: 'C:\\secret\\member' }] };
seam.State.importRows = [{ selected: true, path: 'C:\\secret\\member' }];
assert.strictEqual(seam.persistImportDraftReference(), true);
assert.strictEqual(saves.length, 1);
assert.strictEqual(saves[0].id, 'project-import');
assert.deepStrictEqual(JSON.parse(saves[0].body), {
  schema: 'vcstudio.safe-draft-ref/v1', kind: 'project-import',
});
for (const forbidden of ['secret', 'confidential-catalyst', 'private-row-name']) {
  assert.ok(!JSON.stringify(saves[0]).includes(forbidden), `marker leaked ${forbidden}`);
}
assert.strictEqual(document.getElementById('pj-import-cancel').disabled, false);
seam.discardImportDraft();
assert.deepStrictEqual(removals, ['project-import']);
assert.strictEqual(document.getElementById('pj-import-source').value, '');
assert.strictEqual(document.getElementById('pj-import-root').value, '');
assert.strictEqual(seam.State.importRows.length, 0);
""",
        str(ASSETS / "project.js"),
    )


def test_cluster_draft_marker_never_contains_connection_or_credential_fields():
    _run_node(
        r"""
const records = new Map();
const saves = []; const removals = [];
VCS.workspace = {
  drafts: {
    load(id) { return records.get(id) || null; },
    save(id, body, metadata) {
      const record = { schema: 'vcstudio.unverified-draft/v1', id, text: body, metadata };
      records.set(id, record); saves.push({ id, body, metadata }); return record;
    },
    remove(id) { records.delete(id); removals.push(id); },
  },
};
element('cl-cancel', { disabled: true });
element('cl-name', { value: 'private-cluster' });
element('cl-hostname', { value: 'login.secret.example' });
element('cl-username', { value: 'secret-user' });
element('cl-keypath', { value: 'C:\\Users\\secret\\id_rsa' });
element('cl-remoteroot', { value: '/home/secret/work' });
element('cl-vaspcmd', { value: 'TOKEN=credential mpirun vasp_std' });
loadAsset(process.argv[1]);
const seam = window.Cluster && window.Cluster.__test;
assert.ok(seam, 'cluster resume test seam must be exposed');
assert.strictEqual(seam.persistClusterDraftReference(), true);
assert.strictEqual(saves.length, 1);
assert.strictEqual(saves[0].id, 'cluster-profile');
assert.deepStrictEqual(JSON.parse(saves[0].body), {
  schema: 'vcstudio.safe-draft-ref/v1', kind: 'cluster-profile',
});
for (const forbidden of ['private-cluster', 'login.secret.example', 'secret-user',
  'id_rsa', '/home/secret', 'credential']) {
  assert.ok(!JSON.stringify(saves[0]).includes(forbidden), `marker leaked ${forbidden}`);
}
assert.strictEqual(document.getElementById('cl-cancel').disabled, false);
seam.clearClusterDraftReference();
assert.deepStrictEqual(removals, ['cluster-profile']);
assert.strictEqual(document.getElementById('cl-cancel').disabled, true);
""",
        str(ASSETS / "cluster.js"),
    )


def test_resume_center_routes_known_drafts_and_marks_remote_only_refs_unavailable():
    _run_node(
        r"""
const localRecords = new Map([
  ['project-import', { schema: 'vcstudio.unverified-draft/v1',
    text: 'C:\\must-not-render\\private-import' }],
  ['cluster-profile', { schema: 'vcstudio.unverified-draft/v1',
    text: 'password=must-not-render' }],
]);
VCS.workspace = {
  state: { draft_refs: {
    'project-import': { dirty: true, route: '#/home', updated_at_ms: 30 },
    'cluster-profile': { dirty: true, route: '#/home', updated_at_ms: 20 },
    'report-project-a-report': {
      dirty: true, route: '#/publish/report?project=project-a',
      project_id: 'project-a', updated_at_ms: 10,
    },
  } },
  projects: [{ id: 'project-a', name: 'Project A' }],
  drafts: { load(id) { return localRecords.get(id) || null; } },
  routeHash(id) {
    if (id === 'project-overview') return '#/projects/current/overview';
    if (id === 'environment-cluster') return '#/environment/cluster';
    return '#/home';
  },
  parseRoute(route) {
    if (route === '#/projects/current/overview') return { def: { page: 'project' } };
    if (route === '#/environment/cluster') return { def: { page: 'cluster' } };
    if (route === '#/publish/report?project=project-a') {
      return { def: { page: 'report-workbench' } };
    }
    return null;
  },
};
VCS.unsaved = { scopes() { return []; } };
VCS.canActivatePage = () => true;
element('db-resume');
loadAsset(process.argv[1]);
const seam = window.Dashboard && window.Dashboard.__test;
assert.ok(seam, 'dashboard resume test seam must be exposed');
const rows = seam.collectResumeRows();
const project = rows.find(row => row.id === 'project-import');
const cluster = rows.find(row => row.id === 'cluster-profile');
const report = rows.find(row => row.id === 'report-project-a-report');
assert.deepStrictEqual(
  { route: project.route, status: project.status, available: project.available },
  { route: '#/projects/current/overview', status: 'current', available: true });
assert.deepStrictEqual(
  { route: cluster.route, status: cluster.status, available: cluster.available },
  { route: '#/environment/cluster', status: 'current', available: true });
assert.strictEqual(report.status, 'unavailable');
assert.strictEqual(report.available, false);
seam.renderResumeCenter();
const markup = document.getElementById('db-resume').innerHTML;
assert.ok(markup.includes('当前设备可继续'));
assert.ok(markup.includes('当前设备不可用'));
assert.ok(markup.includes('#/projects/current/overview'));
assert.ok(markup.includes('#/environment/cluster'));
assert.ok(markup.includes('仅原设备可继续'));
assert.ok(!markup.includes('must-not-render'));
assert.ok(!markup.includes('password='));
""",
        str(ASSETS / "dashboard.js"),
    )


def test_resume_buttons_and_form_entry_points_are_wired():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    project = (ASSETS / "project.js").read_text(encoding="utf-8")
    cluster = (ASSETS / "cluster.js").read_text(encoding="utf-8")
    assert 'id="pj-import-cancel"' in html
    assert "wire('pj-import-cancel', discardImportDraft)" in project
    assert "e.detail.source === 'resume-center'" in project
    assert 'id="cl-cancel"' in html
    assert "wire('cl-cancel', discardClusterDraft)" in cluster
    assert "event.detail.source === 'resume-center'" in cluster
