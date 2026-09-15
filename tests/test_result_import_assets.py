"""本地 VASP 结果导入向导的前端静态契约。

交互行为由 pywebview 执行；这里守住 API 名、关键门禁和引导控件，JavaScript 语法
另由 ``node --check`` 覆盖。
"""
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' / 'assets'


def _source(name):
    return (ASSETS / name).read_text(encoding='utf-8')


def test_result_import_is_guided_primary_adsorption_entry():
    html = _source('index.html')
    assert html.index('id="pj-import-card"') < html.index('id="pj-create"')
    for control in (
        'pj-import-source-btn', 'pj-import-scan', 'pj-import-summary',
        'pj-import-filter', 'pj-import-search', 'pj-import-confirm-selected',
        'pj-import-name', 'pj-import-root', 'pj-import-commit', 'pj-import-done',
    ):
        assert f'id="{control}"' in html
    # 老的从零生成流程保留，但默认折叠，减少首屏认知负担。
    assert 'data-acc="project:create-adsorption"' in html
    assert 'data-acc-default="0"' in html


def test_result_import_calls_scan_then_commit_with_full_review_payload():
    js = _source('project.js')
    assert "VCS.call('proj_import_scan', source)" in js
    assert "VCS.call('proj_import_commit', source, root, name, importSelections())" in js
    for field in ('path:', 'selected:', 'role:', 'task_type:', 'manual_confirm:',
                  'confirmation_reason:', 'species:'):
        assert field in js
    assert 'confirmationEligible' in js
    assert 'row.manualConfirm && row.confirmationEligible' in js
    assert 'const deltaResult = await delta()' in js
    assert "? '用这些参考能开始吸附计算'" in js
    assert 'window.Project = {' in js
    for member in ('reload: reloadProjects', 'selectByPath', 'selectByName',
                   'openImport', 'startLiS'):
        assert member in js


def test_molecule_reference_only_import_is_not_misreported_as_report_ready():
    js = _source('project.js')
    assert 'const isAdsorption = gate.clean > 0 && gate.configs > 0' in js
    assert 'const reportReady = isAdsorption && !hasNeedsHuman && !hasIncompleteDelta' in js
    assert '用这些参考能开始吸附计算' in js


def test_result_import_supports_adsorption_molecules_and_standalone_results():
    js = _source('project.js')
    for role in ('clean_slab', 'config', 'gas_ref', 'molecule_ref', 'standalone', 'ignore'):
        assert role in js
    assert '一个吸附能项目只能选择 1 个清洁表面' in js
    assert '分子参考未填写物种名称' in js
    assert '不能人工跳过' in js
    assert '提交时仍会复核硬性门禁' in js
    assert '自动检查已通过，无需人工确认' in js
    assert '可直接导入；提交时仍会复核源文件是否变化' in js


def test_species_reference_import_requires_species_for_every_config():
    js = _source('project.js')
    assert 'const usesSpeciesRefs = molecules > 0' in js
    assert "selected.filter(row => row.role === 'config' && !row.species.trim())" in js
    assert '已选择逐物种分子参考：请为' in js
    assert '吸附物种，如 Li2S8（使用分子参考时必填）' in js


def test_manual_confirmation_carries_editable_audit_reason():
    js = _source('project.js')
    assert "const DEFAULT_CONFIRMATION_REASON = '已核对原始 OUTCAR/OSZICAR 与末结构，确认该任务收敛'" in js
    assert "String(row.confirmationReason || '').trim()) return 'ready'" in js
    assert 'data-act="confirm-reason"' in js
    assert 'row.confirmationReason = DEFAULT_CONFIRMATION_REASON' in js
    assert 'confirmation_reason: row.manualConfirm ? row.confirmationReason.trim() : null' in js
    assert '人工确认结果尚未填写核对依据' in js
    assert '.pj-import-confirm-reason' in _source('app.css')


def test_result_import_has_scan_summary_filters_and_responsive_layout():
    css = _source('app.css')
    for selector in (
        '.pj-import-flow', '.pj-import-summary', '.pj-import-attention',
        '.pj-import-table-wrap', '.pj-import-commitbar', '.pj-import-done',
    ):
        assert selector in css
    assert '@media(max-width:820px)' in css


def test_dashboard_routes_inputs_and_results_to_distinct_guided_flows():
    js = _source('dashboard.js')
    # 首页入口按工作模式动态渲染；导入结果与提交输入仍是两条独立路径。
    assert 'import_adsorption_results:' in js and 'openResultsImport]' in js
    assert 'submit_inputs:' in js and 'openInputsSubmit]' in js
    assert 'ACTIONS[b.dataset.action]' in js
    assert 'window.Project.openImport();' in js
    assert "const card = $('quick-submit-card')" in js
    assert "const first = $('qs-add-dir')" in js
    assert "openImport('inputs')" not in js
    assert "openImport('results')" not in js


def test_input_only_quartets_are_visible_and_continue_to_real_submission():
    project = _source('project.js')
    jobs = _source('jobs.js')
    html = _source('index.html')
    app = _source('app.js')
    assert "=== 'CREATED'" in project
    assert '四件套完整，待提交' in project
    assert 'input_complete !== false' in project
    assert "!== 'DONE' && !isCreatedInput(row)" in project
    assert "status === 'ready' && !isCreatedInput(row)" in project
    assert '分子参考仅接收 DONE 结果或已验证的完整四件套待提交' in project
    assert 'id="pj-import-filter"' in html and 'value="created"' in html
    assert 'createdCount' in project
    assert 'openImportedCreatedJobs' in project
    assert 'window.Jobs.selectCreatedProject' in project
    assert 'async function selectCreatedProject(projectPath)' in jobs
    assert "row.state === 'CREATED'" in jobs
    assert "CREATED: ['q', '待提交']" in app
