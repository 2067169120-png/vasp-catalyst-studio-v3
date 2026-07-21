"""一站式 Li-S 吸附计算前端的静态合同。"""
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' / 'assets'


def _source(name):
    return (ASSETS / name).read_text(encoding='utf-8')


def test_lis_builder_guides_reference_inputs_output_and_resources():
    html = _source('index.html')
    for control in (
        'pj-create-card', 'lis-reference', 'lis-reference-status',
        'lis-input-dir', 'lis-bundle-status',
        'pj-incar', 'pj-slab', 'pj-cfg-add', 'pj-cfg-dir', 'pj-cfg-list',
        'lis-bulk-species', 'lis-apply-species', 'lis-only-unmatched',
        'pj-name', 'pj-root', 'lis-profile', 'lis-cores', 'lis-walltime',
        'lis-resource-summary',
        'pj-submit-all', 'lis-readiness', 'lis-submit-result',
    ):
        assert f'id="{control}"' in html
    assert '生成并提交整组，开启自动续算/下载/报告' in html
    assert '只生成，不提交（兼容旧流程）' in html
    assert '最多续算 3 轮' in html
    assert 'OUTCAR、OSZICAR、CONTCAR 3 个文件' in html


def test_lis_builder_uses_backend_contract_and_strict_species_mapping():
    js = _source('project.js')
    assert "VCS.call('proj_scan_lis_inputs', picked.path)" in js
    assert "VCS.call('proj_scan_structures', picked.path)" in js
    assert "VCS.call('proj_prepare_lis', val('pj-name'), val('pj-slab'), gate.items," in js
    assert "VCS.call('submit_project_with_resources', projectPath," in js
    assert 'State.configSpecies' in js
    assert 'reference_species' in js
    assert 'n_species_refs' in js
    assert 'const invalidSpecies = items.filter' in js
    assert "species: String(State.configSpecies[path] || '').trim()" in js
    # 兼容旧流程：原 proj_create 仍收 string[]，不改既有参数顺序。
    assert 'const configs = State.configs.slice()' in js
    assert "VCS.call('proj_create', name, slab, configs, incar, gas, root)" in js


def test_one_folder_input_import_only_autofills_unambiguous_members():
    html = _source('index.html')
    js = _source('project.js')
    assert '导入本次计算文件夹（推荐）' in html
    assert '有歧义时只提示你确认，不会擅自选择' in html
    assert 'if (result.incar) setVal' in js
    assert 'if (result.clean_slab)' in js
    assert 'removeConfigPath(result.clean_slab)' in js
    assert '原始文件没有被修改' in js


def test_large_config_folders_have_safe_bulk_species_tools():
    js = _source('project.js')
    css = _source('app.css')
    assert 'function applyBulkSpecies()' in js
    assert '应用到全部未匹配项' in _source('index.html')
    assert '只看未匹配' in _source('index.html')
    assert '已匹配 ${matched}/${State.configs.length} 个构型' in js
    assert '<select class="ipt lis-species"' in js
    assert '.lis-config-field .pj-cfglist' in css and 'max-height:' in css


def test_lis_submission_handles_password_host_trust_and_starts_pipeline():
    js = _source('project.js')
    assert "r.error === 'NEED_PASSWORD'" in js
    assert 'r.needPassword' in js
    assert 'r.needs_trust' in js
    assert 'VCS.password()' in js
    assert 'VCS.confirmHostKey(' in js
    assert 'VCS.pipeline.reconfigure' in js
    assert 'submissionRows(submitted)' in js
    assert '请保持软件运行' in js


def test_import_completion_can_start_lis_with_preselected_references():
    js = _source('project.js')
    assert '用这些参考能开始吸附计算' in js
    assert 'startLiS(' in js
    assert 'window.Project = {' in js
    for member in ('reload: reloadProjects', 'selectByPath', 'selectByName',
                   'openImport', 'startLiS'):
        assert member in js
    assert "sel.value = hit.path; onReferenceChanged()" in js


def test_reference_only_import_is_not_mislabelled_as_missing_delta_e():
    js = _source('project.js')
    assert 'const referenceOnly = gate.molecules > 0 && !isAdsorption' in js
    assert 'Li-S 参考能库已建立，可以开始新的吸附计算' in js
    assert "? '用这些参考能开始吸附计算'" in js
    assert "referenceOnly ? 'Li-S 参考能库已就绪。'" in js


def test_cross_file_energy_evidence_is_visible_to_user():
    js = _source('project.js')
    assert 'raw.cross_file_energy' in js
    assert 'cross.spread_eV' in js
    assert '跨文件末能量一致' in js
    assert '跨文件末能量不一致' in js
    assert '最大差' in js


def test_lis_builder_has_guided_and_responsive_styles():
    css = _source('app.css')
    for selector in (
        '.lis-builder', '.lis-step', '.lis-step.ready', '.lis-cfgrow.invalid',
        '.lis-resource-grid', '.lis-automation-note', '.lis-readiness.ready',
        '.lis-submit-result', '.lis-result-next',
    ):
        assert selector in css
    assert '@media(max-width:680px)' in css


def test_adsorption_is_the_default_low_cognitive_load_workflow():
    html = _source('index.html')
    assert html.index('<option value="adsorption"') < html.index('<option value="taskana"')
    for control in (
        'ads-journey', 'ads-journey-status', 'ads-route-import',
        'ads-route-new', 'ads-route-results', 'pj-import-problem',
        'pj-results-card', 'pj-project-summary',
    ):
        assert f'id="{control}"' in html
    assert 'data-flow-step="6"' in html
    assert '专家 / 旧流程' in html
    assert 'data-acc="project:import-results"' in html
    assert 'data-acc="project:results"' in html


def test_lis_progressively_opens_only_the_current_step():
    html = _source('index.html')
    js = _source('project.js')
    css = _source('app.css')
    for step in range(1, 5):
        assert f'data-lis-open-step="{step}"' in html
    assert 'const firstIncomplete = [1, 2, 3, 4].find' in js
    assert "el.classList.toggle('current', step === activeStep)" in js
    assert '请先完成第 ${firstIncomplete} 步' in js
    assert '.lis-step:not(.current) .lis-step-body' in css
    assert 'content:"当前步骤"' in css


def test_prepared_project_is_reused_for_cancelled_or_partial_submit():
    js = _source('project.js')
    assert 'preparedLis: null' in js
    assert 'function lisContentFingerprint()' in js
    assert 'function reusablePreparedLis()' in js
    assert 'if (reusable)' in js
    assert "prepared = { ok: true, project_path: reusable.path" in js
    assert 'State.preparedLis = {' in js
    assert 'State.preparedLis.submitted = true' in js
    assert '重试未提交成员（不会重复生成）' in js
    assert '输入已改变，不能复用刚才生成的项目' in js
    assert '请把项目名改成新名称' in js


def test_method_check_requires_explicit_auditable_confirmation():
    html = _source('index.html')
    js = _source('project.js')
    for control in ('lis-method-check', 'lis-method-confirm', 'lis-method-reason'):
        assert f'id="{control}"' in html
    assert 'checked' not in html[html.index('id="lis-method-confirm"') - 80:
                                 html.index('id="lis-method-confirm"') + 80]
    assert 'gate.ref.path, gate.methodConfirmation' in js
    assert 'prepared.needs_method_confirmation' in js
    assert "methodStatus === 'incompatible'" in js
    assert '请填写可审计的方法一致性确认理由' in js


def test_host_trust_never_blindly_accepts_missing_fingerprint():
    app = _source('app.js')
    project = _source('project.js')
    cluster = _source('cluster.js')
    jobs = _source('jobs.js')
    assert 'async confirmHostKey(result)' in app
    assert "fingerprint.startsWith('SHA256:')" in app
    assert 'return ok ? { host, fingerprint, algorithm } : null' in app
    assert 'trust = true' not in '\n'.join((project, cluster, jobs))
    for source in (project, cluster, jobs):
        assert 'VCS.confirmHostKey(' in source


def test_delta_and_project_summary_show_species_reference_truthfully():
    js = _source('project.js')
    assert 'function updateProjectSummary(deltaResult)' in js
    assert "['species', 'species_refs'].includes(referenceMode)" in js
    assert '逐物种参考（具体物种见 ΔE 表）' in js
    assert '参考物种 / E_ref (eV)' in js
    assert 'E_config − E_slab − E_ref(' in js
    assert 'project.n_done' in js
    assert 'ΔΔE (eV)' in js
    assert '最稳构型' in js
    assert 'function deltaRepair(row)' in js
    assert '下一步：核对下表后生成完整 HTML 报告' in js


def test_submitted_but_unattended_credential_failure_is_not_called_autopilot():
    js = _source('project.js')
    assert 'const automationNotReady =' in js
    assert '任务已提交，但自动续算尚未接管' in js
    assert '任务已经提交，不要重新生成项目' in js


def test_global_autopilot_fails_closed_until_a_managed_submit_enables_it():
    app_js = _source('app.js')
    settings_js = _source('settings.js')
    assert 'const enabled = ui.autopilot === true' in app_js
    assert 'ui.autopilot !== false' not in app_js
    assert "$('set-ap-on').checked = ui.autopilot === true" in settings_js


def test_user_facing_copy_calls_it_managed_workflow_not_autopilot():
    visible = '\n'.join(_source(name) for name in (
        'index.html', 'app.js', 'project.js', 'settings.js'))
    assert '自动驾驶' not in visible
    assert '自动托管' in visible
    assert '监控、续算、下载和报告' in visible


def test_delta_view_blocks_method_mismatch_and_guides_unverified_results():
    js = _source('project.js')
    css = _source('app.css')
    assert 'r.method_consistency || {}' in js
    assert "methodStatus === 'incompatible'" in js
    assert '方法不一致：ΔE 已阻断' in js
    assert "methodStatus === 'unverified'" in js
    assert '方法一致性尚未完全核验' in js
    assert '统一所有相减项的泛函、ENCUT、色散和 POTCAR 后重算' in js
    assert '.pj-method-gate.incompatible' in css
    assert '.pj-method-gate.unverified' in css


def test_folder_scan_prefers_valid_final_contcar_and_explains_override():
    js = _source('project.js')
    assert 'function preferredScannedStructures(raw)' in js
    assert "item.valid === false || item.parseable === false" in js
    assert "if (base === 'contcar') return 20" in js
    assert "if (base === 'poscar') return 10" in js
    assert 'item.preferred === true' in js
    assert '已自动选择可用的最终 CONTCAR' in js
    assert '如需原始 POSCAR 请使用“逐个添加”' in js


def test_submit_button_stays_locked_while_async_method_check_or_submit_runs():
    js = _source('project.js')
    assert 'lisBusy: false' in js
    assert 'button.disabled = State.lisBusy || !gate.ok' in js
    assert 'State.lisBusy = true' in js
    assert 'State.lisBusy = false' in js


def test_errors_offer_a_direct_repair_action_instead_of_log_only():
    js = _source('project.js')
    assert 'function lisRepairHint(message, stage)' in js
    assert 'function lisRepairAction(message, stage)' in js
    assert 'data-lis-fix=' in js
    assert "return ['cluster', '前往集群配置']" in js
    assert "return ['step3', '返回修改项目名']" in js
    assert "return ['method', '查看方法检查']" in js


def test_project_progress_is_restored_after_reopening_the_app():
    js = _source('project.js')
    html = _source('index.html')
    assert "CURRENT_PROJECT_KEY = 'vcs.adsorption.current_project'" in js
    assert "VCS.call('pipeline_status')" in js
    assert 'function restoreWorkflowState(project)' in js
    assert 'pipeline_needs_human' in js
    assert 'localStorage.setItem(CURRENT_PROJECT_KEY' in js
    assert '处理任务异常' in js
    assert '<b>5</b>监控与续算' in html


def test_multiple_servers_are_visible_in_pipeline_and_event_feed():
    dashboard = _source('dashboard.js')
    app = _source('app.js')
    assert 'p.profile' in dashboard and 'pl-cluster' in dashboard
    assert 'e.cluster' in app and 'fcluster' in app
    assert "fetch: '下载'" in app


def test_server_selection_prefills_resources_and_explains_multi_server_monitoring():
    js = _source('project.js')
    html = _source('index.html')
    assert 'function applyLisProfileDefaults()' in js
    assert "setVal('lis-cores', String(Number(profile.ppn)))" in js
    assert "setVal('lis-walltime', profile.walltime)" in js
    assert '其他项目可同时选择别的服务器，软件会并行监控' in js
    assert '单个作业核数' in html
