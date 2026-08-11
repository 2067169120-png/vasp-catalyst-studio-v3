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
        'pj-incar', 'pj-slab', 'lis-clean-incar-status',
        'pj-cfg-add', 'pj-cfg-dir', 'pj-cfg-list',
        'lis-bulk-species', 'lis-apply-species', 'lis-only-unmatched',
        'pj-name', 'pj-root', 'lis-profile', 'lis-cores', 'lis-walltime',
        'lis-resource-summary',
        'pj-submit-all', 'lis-readiness', 'lis-submit-result',
    ):
        assert f'id="{control}"' in html
    assert '生成并提交整组，开启自动续算/下载/报告' in html
    assert '只生成当前逐目录作业，不提交' in html
    assert '最多续算 3 轮' in html
    assert 'OUTCAR、OSZICAR、CONTCAR 3 个文件' in html


def test_lis_builder_uses_backend_contract_and_strict_species_mapping():
    js = _source('project.js')
    assert "'proj_scan_lis_inputs', picked.path, selectedReferenceSpecies()" in js
    assert "'proj_scan_structures', picked.path, val('pj-slab'), selectedReferenceSpecies()" in js
    assert "VCS.call('proj_prepare_lis', val('pj-name'), val('pj-slab'), gate.items," in js
    assert "VCS.call('proj_resolve_member_incar', path, val('pj-incar'))" in js
    assert "VCS.call('submit_project_with_resources', projectPath," in js
    assert 'State.configSpecies' in js
    assert 'reference_species' in js
    assert 'n_species_refs' in js
    assert 'const invalidSpecies = items.filter' in js
    assert "species: String(State.configSpecies[path] || '').trim()" in js
    assert "incar_path: String(meta.incarPath || '')" in js
    assert 'gate.memberIncars' in js
    assert 'quartet: meta.quartet' in js
    assert 'input_mode: String(meta.inputMode' in js
    assert 'quartet_status: String(meta.quartetStatus' in js
    # 专家“只生成”也必须复用同一逐成员契约，不能退回共享 INCAR API。
    create_block = js[js.index('async function create()'):js.index('// ── 已有项目')]
    assert "'proj_prepare_lis', val('pj-name'), val('pj-slab'), gate.items" in create_block
    assert 'gate.memberIncars' in create_block
    assert "VCS.call('proj_create'" not in create_block


def test_one_folder_input_import_only_autofills_unambiguous_members():
    html = _source('index.html')
    js = _source('project.js')
    assert '导入本次计算文件夹（推荐）' in html
    assert '完整组原样使用，不完整组按本目录 POSCAR+INCAR 生成受管四件套' in html
    assert '本目录文件始终优先' in html
    assert "const rootFallback = String(result.incar || '').trim()" in js
    assert "setVal('pj-incar', rootFallback)" in js
    assert "setVal('pj-slab', String(result.clean_slab || ''))" in js
    assert 'if (result.clean_slab)' in js
    assert 'clean_incar_sha256' in js
    assert 'item.incar_path' in js
    assert 'removeConfigPath(result.clean_slab)' in js
    assert '原始文件没有被修改' in js


def test_large_config_folders_have_safe_bulk_species_tools():
    js = _source('project.js')
    css = _source('app.css')
    assert 'function applyBulkSpecies()' in js
    assert '应用并确认全部未确认项' in _source('index.html')
    assert '只看未确认' in _source('index.html')
    assert '物种已确认 ${matched}/${State.configs.length}，INCAR 已绑定 ${incars}/${State.configs.length}' in js
    assert '确认本组映射' in js
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
    assert '专家：按当前逐目录输入' in html
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


def test_method_check_separates_execution_from_energy_comparability():
    html = _source('index.html')
    js = _source('project.js')
    for control in (
        'lis-method-check', 'lis-method-outcome', 'lis-method-issues',
        'lis-method-notes', 'lis-method-warnings', 'lis-method-repairs',
    ):
        assert f'id="{control}"' in html
    assert 'id="lis-method-confirm"' not in html
    assert 'id="lis-method-reason"' not in html
    assert 'gate.ref.path, gate.methodConfirmation' in js
    assert 'prepared.needs_method_confirmation' in js
    assert 'check.execution_status' in js
    assert 'check.comparability_status || check.status' in js
    gate = js[js.index('function lisGate()'):js.index('function updateLisReadiness(')]
    assert "const methodBlocked = executionStatus === 'blocked'" in gate
    assert 'methodNeedsConfirmation' not in gate
    assert 'methodConfirmation: null' in gate
    assert '作业生成 / 提交：可继续' in js
    assert '自动 ΔE / 最终报告：已暂停' in js
    assert '输入执行检查已阻止生成 / 提交' in js


def test_ispin_notes_and_managed_copy_repairs_are_informational():
    js = _source('project.js')
    method = js[js.index('function renderMethodCheck('):
                js.index('function renderRepairPlan(')]
    assert 'check.notes' in method
    assert "'lis-method-notes', '体系说明（包括 ISPIN）'" in method
    assert "'lis-method-repairs', '已在受管副本安全修复（源文件未改）'" in method
    assert 'checkbox' not in method
    repair = js[js.index('function renderRepairPlan('):
                js.index('function invalidatePreparedLis(')]
    assert "action.risk || '').toLowerCase() === 'low'" in repair
    assert 'MAGMOM、ISPIN 等科学选择只给建议，不自动改' in repair
    assert '仅建议，不自动' in repair
    assert 'VCS.call(' not in repair


def test_each_member_shows_copied_or_generated_quartet_evidence():
    html = _source('index.html')
    js = _source('project.js')
    css = _source('app.css')
    assert 'POSCAR/INCAR/KPOINTS/POTCAR 会原样绑定' in html
    for field in ('raw.quartet', 'raw.input_mode', 'raw.quartet_status'):
        assert field in js
    assert 'function quartetPresentation(raw, fallbackIncar)' in js
    assert '完整四件套原样绑定（源文件不改）' in js
    assert '将以本目录 POSCAR+INCAR 生成受管四件套；源目录不改' in js
    assert '四件套不可提交' in js
    assert 'quartetBlocked(State.cleanIncar)' in js
    assert 'items.filter(quartetBlocked)' in js
    assert '.lis-quartet.generate' in css
    assert '.lis-quartet.blocked' in css


def test_same_local_path_preserves_posix_case_and_folds_windows_drive_paths():
    js = _source('project.js')
    helper = js[js.index('function normaliseLocalPath('):
                js.index('function removeConfigPath(')]
    assert "replace(/\\\\/g, '/').replace(/\\/+$/, '')" in helper
    assert "return /^[A-Za-z]:(?:\\/|$)/.test(normalised) ? normalised.toLowerCase() : normalised;" in helper
    assert 'return normaliseLocalPath(left) === normaliseLocalPath(right);' in helper
    assert ".replace(/\\/+$/, '').toLowerCase()" not in helper


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
    assert '后台会自动生成 HTML、Word 与 PDF' in js


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
    assert '合法的 ISPIN 差异不属于硬冲突' in js
    assert '体系自旋提示（不阻断 ΔE）' in js
    assert '分子参考与周期体系 ISPIN 不同' in js
    assert '.pj-method-gate.incompatible' in css
    assert '.pj-method-gate.unverified' in css


def test_new_input_folder_scan_prefers_poscar_and_explains_contcar_fallback():
    js = _source('project.js')
    assert 'function preferredScannedStructures(raw)' in js
    assert "item.valid === false || item.parseable === false" in js
    assert "if (base === 'poscar') return 20" in js
    assert "if (base === 'contcar') return 10" in js
    assert 'item.preferred === true' in js
    assert '已优先选择本次输入 POSCAR' in js
    assert '请确认不是旧结果残留' in js


def test_submit_button_stays_locked_while_async_method_check_or_submit_runs():
    js = _source('project.js')
    assert 'lisBusy: false' in js
    assert 'inputScanBusy: false' in js
    assert 'inputGeneration: 0' in js


def test_new_bundle_replaces_old_group_atomically_and_discards_stale_scans():
    js = _source('project.js')
    bundle = js[js.index('async function addLisInputDirectory()'):
                js.index('function lisGate()')]
    scan_call = bundle.index("'proj_scan_lis_inputs', picked.path")
    assert bundle.index('const generation = ++State.inputGeneration') < scan_call
    assert bundle.index('State.configs = []') < scan_call
    assert bundle.index("setVal('pj-slab', '')") < scan_call
    assert bundle.index("setVal('pj-incar', '')") < scan_call
    assert 'if (generation !== State.inputGeneration) return' in bundle
    assert 'State.inputScanBusy = true' in bundle
    config_scan = js[js.index('async function addConfigDirectory()'):
                     js.index('function updateLisResourceSummary()')]
    assert 'const generation = State.inputGeneration' in config_scan
    assert 'if (generation !== State.inputGeneration) return' in config_scan


def test_project_report_requests_backend_final_adsorption_gate():
    js = _source('project.js')
    assert "VCS.call('proj_report', proj.path, save, true)" in js
    assert 'button.disabled = State.lisBusy || State.inputScanBusy || !gate.ok' in js
    assert 'State.lisBusy = true' in js
    assert 'State.lisBusy = false' in js


def test_single_report_format_selection_is_accessible_and_enforced():
    html = _source('index.html')
    js = _source('project.js')
    css = _source('app.css')

    assert '<fieldset class="pj-report-formats" id="pj-report-formats"' in html
    assert 'aria-describedby="pj-report-format-status"' in html
    assert 'aria-describedby="pj-report-format-status" aria-busy="true"' in html
    for control, label in (
            ('pj-report-format-html', 'HTML'),
            ('pj-report-format-docx', 'Word（DOCX）'),
            ('pj-report-format-pdf', 'PDF')):
        assert f'id="{control}"' in html
        assert f'for="{control}"' in html
        assert label in html
    assert 'id="pj-report-format-status" role="status"' in html
    assert 'aria-live="polite" aria-atomic="true"' in html
    assert '已选择：HTML；能力检测中：DOCX、PDF' in html
    for control in ('pj-report-format-docx', 'pj-report-format-pdf'):
        start = html.index(f'<input type="checkbox" id="{control}"')
        end = html.index('>', start)
        initial_control = html[start:end]
        assert ' disabled' in initial_control
        assert ' checked' not in initial_control
        assert 'aria-disabled="true"' in initial_control
    assert 'function selectedReportFormats()' in js
    assert 'function requireReportFormats()' in js
    assert "VCS.call('proj_report_capabilities')" in js
    capability = js[js.index('function initialReportCapabilities()'):
                    js.index('function requireReportFormats()')]
    assert "reportCapabilities: initialReportCapabilities()" in js
    assert "reportCapabilityState: 'pending'" in js
    assert "html: { available: true, reason: '' }" in capability
    assert capability.count('available: false') >= 2
    assert 'capability.available === true' in capability
    assert 'disabledByCapability = !(capability && capability.available === true)' in capability
    assert "State.reportCapabilityState = 'failed'" in capability
    assert "applyReportCapabilities(result.formats)" in capability
    assert "setReportCapabilityFailure(" in capability
    assert "description.textContent = disabledByCapability ? reason" in capability
    assert 'unavailableReportFormatText(unavailable)' in capability
    assert "fieldset.setAttribute(\n        'aria-busy'" in capability
    init = js[js.index("wire('pj-report', () => openReportWorkbench('report'));"):]
    assert init.index('\n    syncReportFormatControls();') < init.index('\n    loadReportCapabilities();')
    assert 'if (!selectedFormats) return' in js
    assert "'proj_report_bundle', proj.path, dr.path, selectedFormats, true" in js
    assert '请至少选择一种报告格式' in js
    assert "selectedFormats.length !== 1 || selectedFormats[0] !== 'html'" in js
    assert '请只勾选 HTML 后重试' in js
    assert "kind: 'final'" not in js[js.index('if (bridgeMethodUnavailable(r))'):
                                      js.index("VCS.log('生成完整报告失败:")]
    assert "first.focus()" in js
    assert '.pj-report-format-options' in css
    assert '.pj-report-formats.invalid' in css


def test_report_artifact_and_scientific_states_are_rendered_separately():
    project = _source('project.js')
    dashboard = _source('dashboard.js')
    css = _source('app.css')

    science = project[project.index('function reportScienceState('):
                      project.index('function reportStateMarkup(')]
    pipeline = dashboard[dashboard.index('function pipelineReportStates('):
                         dashboard.index('function shortTime(')]
    assert 'result.scientific_status || result.report_status || result.report_kind || result.kind' in science
    assert 'report_done' not in science
    assert '<b>报告产物</b>' in project and '<b>科学状态</b>' in project
    assert '<b>发布门禁</b>' in project
    assert "p.stage === 'report_done'" in pipeline
    assert 'report_done 只说明文件生成流程结束，绝不用于推断科学结论是 final' in pipeline
    assert 'p.scientific_status || p.report_status || p.report_kind' in pipeline
    assert '<b>报告产物</b>' in dashboard and '<b>科学状态</b>' in dashboard
    assert '<b>发布门禁</b>' in dashboard
    assert 'p.publication_gate_status' in pipeline
    assert "artifactRaw === 'stale'" in pipeline
    assert '.pj-report-states' in css and '.pl-report-states' in css


def test_errors_offer_a_direct_repair_action_instead_of_log_only():
    js = _source('project.js')
    assert 'function lisRepairHint(message, stage)' in js
    assert 'function lisRepairAction(message, stage)' in js
    assert 'data-lis-fix=' in js
    assert "return ['cluster', '前往集群配置']" in js
    assert "return ['step3', '返回修改项目名']" in js
    assert "return ['method', '查看 ΔE / 报告门禁']" in js


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


def test_workflow_distinguishes_pending_submit_analysis_and_written_report():
    js = _source('project.js')
    restore = js[js.index('function restoreWorkflowState(project)'):
                 js.index('function updateJourney()')]
    assert "State.workflowPendingSubmit = stage === 'submit'" in restore
    assert "State.workflowSubmitted = ['monitor', 'recover'].includes(stage)" in restore
    assert "State.workflowAnalysisReady = ['analysis', 'report_done'].includes(stage)" in restore
    assert "State.workflowResultReady = stage === 'report_done'" in restore
    delta = js[js.index('async function delta()'):js.index('function fmt(')]
    assert 'State.workflowAnalysisReady = !!' in delta
    assert "State.workflowResultReady = State.workflowStage === 'report_done'" in delta
    assert 'State.workflowResultReady = !!' not in delta
    report = js[js.index('async function report()'):js.index('// ── 一键成稿包')]
    assert 'await reloadProjects(proj.path)' in report
    assert '作业已生成，等待提交' in js
    assert '此时自动监控尚未开始' in js


def test_programmatic_project_selection_restores_matching_pipeline_state():
    js = _source('project.js')
    commit = js[js.index('async function commitImport()'):
                js.index('function showImportDone(')]
    assert 'restoreWorkflowState(hit)' in commit
    open_results = js[js.index('async function openProjectResults('):
                      js.index('function canonicalRole(')]
    assert ('restoreWorkflowState(hit)' in open_results or
            'applyProjectSelection(hit)' in open_results)
    selector = js[js.index('async function selectRequestedProject('):
                  js.index('function current()')]
    assert 'applyProjectSelection(hit)' in selector
    assert "return selectRequestedProject('path', wanted, wanted)" in selector
    assert "return selectRequestedProject('name', name, '')" in selector
    apply_selection = js[js.index('function applyProjectSelection('):
                         js.index('async function requestProjectSelection(')]
    assert 'restoreWorkflowState(project || null)' in apply_selection


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
