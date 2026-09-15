"""吸附能优先工作台静态接线测试（浏览器行为由 pywebview 端到端手验）。"""
from pathlib import Path


ASSETS = Path(__file__).parents[1] / 'vcstudio' / 'gui_web' / 'assets'


def _read(name):
    return (ASSETS / name).read_text(encoding='utf-8')


def test_adsorption_workbench_is_primary_project_entry():
    html = _read('index.html')
    assert 'id="ads-workbench"' in html
    assert 'id="ads-import-results"' in html
    assert 'id="ads-import-inputs"' in html
    assert 'id="ads-import-molecules"' in html
    assert 'id="ads-new-from-structure"' in html
    assert html.index('<option value="adsorption"') < html.index('<option value="taskana"')
    assert '<script src="adsorption-workbench.js"></script>' in html


def test_workbench_uses_read_only_scan_preview_commit_sequence():
    js = _read('adsorption-workbench.js')
    assert "VCS.call('proj_import_scan'" in js
    assert "VCS.call('proj_import_preview'" in js
    assert "VCS.call('proj_import_commit'" in js
    assert "if (model.phase === 'map') await runPreview()" in js
    assert "else await commit()" in js
    assert '源目录只读' in js and '不会被修改' in _read('index.html')


def test_workbench_supports_direct_cluster_submission_resources_and_guidance():
    js = _read('adsorption-workbench.js')
    for control in ('ri-profile', 'ri-queue', 'ri-nodes', 'ri-ppn', 'ri-walltime'):
        assert control in js
    assert "VCS.call('test_connection'" in js
    assert "VCS.call('submit_jobs'" in js
    assert "'submission_profile_check'" in js
    assert js.index("'submission_profile_check'") < js.index("VCS.call('proj_import_commit'")
    assert 'resources' in js
    assert '确认导入并直接提交' in js
    assert 'profileIssues' in js and '服务器配置还缺' in js
    assert '查看 ΔE 与报告' in js
    assert 'window.Project.openImport = openImport' in js
    assert '队列 / 分区' in js
    assert "remoteRoot.startsWith('/')" in js


def test_workbench_requires_explicit_scientific_roles():
    js = _read('adsorption-workbench.js')
    assert '请选择且只选择 1 个清洁表面' in js
    assert '请至少选择 1 个吸附构型' in js
    assert '需确认' in js and '不导入' in js
    assert '我已核验' in js


def test_workbench_supports_molecule_only_library_and_explicit_species_mapping():
    html = _read('index.html')
    dashboard = _read('dashboard.js')
    js = _read('adsorption-workbench.js')
    assert 'id="db-start-molecules"' in html
    assert "openImport('molecules')" in dashboard
    assert "projectKind = mode === 'molecules' ? 'molecule_library'" in js
    assert '不会要求清洁面或吸附构型' in js
    assert '每个物种参考态都必须填写化学式' in js
    assert '导入物种参考态后，每个吸附构型都必须明确填写对应物种' in js


def test_workbench_binds_preview_to_commit_and_can_return_to_mapping():
    js = _read('adsorption-workbench.js')
    compact = ''.join(js.split())
    assert 'model.includeLarge,false,model.projectKind)' in compact
    assert 'model.projectKind,(model.plan&&model.plan.fingerprints)||{})' in compact
    assert 'item.force_created=!!row.forceCreated' in compact
    assert 'c.input_complete&&c.source_has_output' in compact
    assert '只用四件套重算' in js
    assert "if (model.phase === 'preview')" in js
    assert "setSecondary('返回修改')" in js
    assert '先导入，稍后配置' in js


def test_project_and_jobs_use_path_identity_for_same_named_projects():
    project = _read('project.js')
    jobs = _read('jobs.js')
    assert 'async function selectByPath(path)' in project
    assert 'window.Project = { reload: reloadProjects, selectByPath, selectByName }' in project
    assert "r.project_path ? 'path:' + r.project_path" in jobs
    assert 'data-proj-path=' in jobs
    assert 'window.Project.selectByPath(path)' in jobs
    assert "allRows.every(row => row.role === 'molecule')" in jobs
    assert '分子参考库' in jobs


def test_adsorption_default_view_exposes_delta_g_figures():
    html = _read('index.html')
    assert 'data-ana="figures adsorption"' in html
    assert 'ΔG 台阶图' in html
