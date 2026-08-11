"""工作模式与“本次计算类型”前端合同测试。"""
from __future__ import annotations

import re
from pathlib import Path

from vcstudio.generate import task_catalog
from vcstudio.gui_web.api import Api
from vcstudio.shared import scenarios


ASSETS = Path(__file__).parents[1] / 'vcstudio/gui_web/assets'


def _read(name: str) -> str:
    return (ASSETS / name).read_text(encoding='utf-8')


def test_every_navigation_page_is_guarded_by_work_mode():
    html = _read('index.html')
    links = re.findall(r'<a\b[^>]*data-page="([^"]+)"[^>]*>', html)
    assert links
    for page in links:
        tag = next(t for t in re.findall(r'<a\b[^>]*>', html)
                   if f'data-page="{page}"' in t)
        assert f'data-scene="pages.{page}"' in tag
    assert 'data-mol-scene' not in html


def test_scene_filter_is_composable_and_programmatic_navigation_is_guarded():
    app = _read('app.js')
    css = _read('app.css')
    assert "toggleAttribute('data-scene-hidden'" in app
    assert '!VCS.canActivatePage(name)' in app
    assert "sceneVisible(sc, 'pages.' + name)" in app
    assert 'VCS.calculationRoute(VCS.activeCalculation, sc)' in app
    assert '[data-scene-hidden]' in css
    assert 'MOL_SCENE_KEYS' not in app


def test_stale_scenario_navigation_cannot_override_the_latest_mode():
    app = _read('app.js')
    assert 'let scenarioNavigationGeneration = 0;' in app
    block = app.split('function keepCurrentPageReachable(sc, source)', 1)[1].split(
        'VCS.applyScenario', 1)[0]
    assert 'const generation = ++scenarioNavigationGeneration;' in block
    assert 'generation !== scenarioNavigationGeneration || VCS.scenario !== sc' in block


def test_settings_put_work_mode_and_exact_calculation_first():
    html = _read('index.html')
    settings = html[html.index('id="page-settings"'):]
    assert settings.index('id="set-scenario"') < settings.index('id="set-llm-provider"')
    assert settings.index('id="set-engine"') < settings.index('id="set-calculation"')
    assert settings.index('id="set-calculation"') < settings.index('id="set-llm-provider"')
    js = _read('settings.js')
    assert "VCS.call('engine_get')" in js
    assert "VCS.call('engine_set'" in js
    assert "VCS.call('calculation_get')" in js
    assert "VCS.call('calculation_set'" in js


def test_engine_selection_hides_other_engines_and_uses_native_units():
    html = _read('index.html')
    app = _read('app.js')
    generate = _read('generate.js')
    css = _read('app.css')
    assert 'data-engine="vasp"' in html
    assert 'data-engine="cp2k gaussian castep"' in html
    assert 'data-engine-hidden' in app and '[data-engine-hidden]' in css
    assert 'id="eng-task"' in html
    assert "VCS.call('calculation_set', sel.value)" in generate
    assert 'id="eng-cutoff-ry"' in html and '单位 Ry' in generate
    assert 'id="eng-cp2k-kpts"' in html and "grid('eng-cp2k-kpts')" in generate
    assert 'id="eng-cutoff"' in html and 'cut_off_energy（eV）' in generate
    assert "boundaries.indexOf('periodic')" in generate
    assert 'periodic.disabled = boundaries.length === 1' in generate


def test_engine_and_task_lists_reload_after_mode_change():
    generate = _read('generate.js')
    taskcat = _read('taskcat.js')
    assert '.filter(e => e.visible !== false)' in generate
    assert "addEventListener('vcs:scenario'" in generate
    assert "VCS.call('task_catalog', sceneKey, active, VCS.activeEngine || 'vasp')" in taskcat
    assert "addEventListener('vcs:engine'" in taskcat
    assert "addEventListener('vcs:calculation'" in taskcat


def test_all_23_catalog_options_have_a_real_action_contract():
    js = _read('taskcat.js')
    derivable_block = re.search(r'const DERIVABLE = new Set\(\[(.*?)\]\);', js, re.S)
    routed_block = re.search(r'const ROUTED = \{(.*?)\n  \};', js, re.S)
    assert derivable_block and routed_block
    derivable = set(re.findall(r"'([a-z0-9_]+)'", derivable_block.group(1)))
    routed = set(re.findall(r'^\s{4}([a-z0-9_]+):', routed_block.group(1), re.M))
    assert {t['key'] for t in task_catalog.CATALOG} == derivable | routed


def test_dashboard_actions_are_driven_by_selected_mode():
    js = _read('dashboard.js')
    assert 'const ACTIONS = {' in js
    assert 'sc.home_actions' in js
    assert "addEventListener('vcs:scenario'" in js
    assert "addEventListener('vcs:calculation'" in js
    assert "VCS.call('task_catalog', sc.key || null, active, VCS.activeEngine || 'vasp')" in js
    assert 'selected_calculation' in js
    assert 'VCS.openCalculation(active' in js


def test_exact_calculation_can_reveal_its_required_page_without_bypassing_whitelist():
    app = _read('app.js')
    lis = scenarios.get_scenario('lis')
    assert 'generate' not in lis['pages']       # Li-S 默认仍保持低认知负担
    assert 'CALCULATION_ROUTES' in app
    assert 'sc.task_keys.indexOf(task) < 0' in app
    assert "page: 'generate'" in app
    assert "link.removeAttribute('data-scene-hidden')" in app
    assert 'function pageAllowed(page, scenario)' in app
    assert "if (sceneVisible(sc, 'pages.' + name)) return true" in app
    assert 'return !!route && route.page === name' in app
    assert 'VCS.canActivatePage = function (page, scenario)' in app
    assert '!VCS.canActivatePage(name)' in app
    assert 'const sceneBlocked =' not in app
    assert 'applySceneElements(VCS.scenario)' in app
    assert 'VCS.openCalculation = async function' in app


def test_settings_explains_selected_task_and_has_one_direct_next_step():
    html = _read('index.html')
    js = _read('settings.js')
    for control in ('set-calculation-guide', 'set-calculation-name',
                    'set-calculation-desc', 'set-calculation-io',
                    'set-calculation-next', 'set-calculation-start'):
        assert f'id="{control}"' in html
    assert 'task.requires' in js and 'task.outputs' in js and 'task.next_action' in js
    assert 'VCS.openCalculation(key' in js


def test_adsorption_entry_distinguishes_one_stop_from_generic_quartet_submit():
    html = _read('index.html')
    project = _read('project.js')
    dashboard = _read('dashboard.js')
    assert 'Li-S 一站式：新建并自动托管' in html
    assert '通用快速提交：已有完整四件套' in html
    assert '不会自动建立吸附能项目' in html
    assert '通用快速提交：已有四件套' in dashboard
    assert "card.getAttribute('data-open') !== '1'" in project
    assert "head.click()" in project


def test_user_facing_assets_never_call_managed_workflow_automatic_driving():
    visible = '\n'.join(_read(name) for name in (
        'index.html', 'app.js', 'dashboard.js', 'project.js', 'settings.js'))
    assert '自动驾驶' not in visible
    assert '自动托管' in visible


def test_specialized_panels_follow_exact_calculation_and_routes_are_callable():
    html = _read('index.html')
    project = _read('project.js')
    scenarios = Path(__file__).parents[1] / 'vcstudio/shared/scenarios.py'
    assert 'id="ads-journey"' in html and 'data-task="adsorption_project"' in html
    assert 'id="fb-card"' in html and 'data-task="formation_binding"' in html
    assert 'id="cd-card"' in html and 'data-task="chgdiff"' in html
    assert 'id="spin-card"' in html and 'data-task="spin_scan"' in html
    assert 'id="ta-spin-jobs"' in html
    assert "addEventListener('vcs:calculation', applyProjectCalculation)" in project
    assert re.search(r'\bstartLiS,?\s*\n?\s*\}', project)
    assert "'project': {'adsorption': True" in scenarios.read_text(encoding='utf-8')


def test_structure_page_hides_unrelated_builders_for_exact_result_tasks():
    html = _read('index.html')
    for marker in ('data-acc="structure:editor"', 'data-acc="structure:mollib"',
                   'id="mslab-card"', 'id="sac-card"'):
        start = html.index(marker)
        tag_start = html.rfind('<div', 0, start)
        tag_end = html.index('>', start)
        tag = html[tag_start:tag_end]
        assert 'data-task=' in tag, marker
        assert 'bands' not in tag and 'dos_pdos' not in tag, marker


def test_non_vasp_generation_leads_directly_to_managed_submission():
    generic = _read('generate.js')
    gaussian = _read('gaussmol.js')
    for source in (generic, gaussian):
        assert 'r.registered' in source
        assert "page: 'jobs'" in source
        assert 'job.yaml' in source


def test_task_analysis_exposes_capability_next_step_and_trace_report():
    html = _read('index.html')
    js = _read('taskcat.js')
    assert 'id="ta-report"' in html
    assert 'ANALYSIS_LABEL' in js and 't.analysis_status' in js
    assert "VCS.call('task_report'" in js
    assert 'vcstudio-task-report.html' in js
    assert 'r.next_action' in js
    assert "addEventListener('vcs:calculation', syncAnalysisKind)" in js


def test_energy_subtraction_calculators_render_scientific_gate_warnings():
    js = _read('taskcat.js')
    assert '(r.warnings || []).forEach' in js
    assert 'runtime.taskcat.surface_energy.warning' in js
    assert "'表面能:{warning}', 'Surface energy: {warning}'" in js
    assert 'runtime.taskcat.formation.warning' in js
    assert "'形成能/结合能:{warning}', 'Formation/binding energy: {warning}'" in js


def test_molecular_mode_has_one_task_selector_and_syncs_gaussian_intent():
    mode = scenarios.get_scenario('molecular')
    assert mode['cards']['generate']['task_catalog'] is False
    assert mode['cards']['generate']['vasp_inputs'] is False
    assert mode['task_keys'] == ['relax', 'static', 'freq']
    js = _read('gaussmol.js')
    assert "const CALCULATION_TASK = { relax: 'opt', static: 'sp', freq: 'freq' }" in js
    assert "addEventListener('vcs:calculation', syncCalculationTask)" in js


def test_every_mode_and_every_declared_calculation_returns_one_actionable_option():
    """遍历设置页的完整模式×计算类型矩阵，避免只测默认选项。"""
    api = Api()
    for mode in scenarios.list_scenarios():
        default = mode['defaults']['active_calculation']
        assert default in mode['task_keys']
        engines = api.engine_list(mode['key'])
        assert engines['error'] is None
        assert {row['key'] for row in engines['engines'] if row['visible']} == set(mode['engines'])
        for key in mode['task_keys']:
            out = api.task_catalog(mode['key'], key)
            assert out['ok'] is True, (mode['key'], key, out.get('error'))
            assert [row['key'] for row in out['tasks']] == [key]
            row = out['tasks'][0]
            assert row['kind_badge']
            assert row['analysis_status'] in {'integrated', 'evidence_only', 'dedicated'}
            assert row['next_action']
            assert row['report_supported'] is True


def test_manual_fetch_defaults_to_each_tasks_own_result_bundle():
    js = _read('jobs.js')
    assert '按任务自动（推荐）' in js
    assert "['CONTCAR', 'OSZICAR', 'OUTCAR']" in js
    assert 'const files = choice.files' in js
    assert "VCS.call('fetch_jobs', dirs, name, pw, trust, files)" in js


def test_jobs_ui_blocks_cross_server_and_repeat_submission_before_password_prompt():
    js = _read('jobs.js')
    assert 'function actionDirs(name, action, mode)' in js
    assert "r.cluster !== name" in js
    assert "r.cluster || r.state !== 'CREATED'" in js
    assert "runtime.jobs.cancel.action_name" in js
    assert re.search(
        r"actionDirs\(name, tr\('runtime\.jobs\.cancel\.action_name'.*?'bound'\)",
        js,
        re.S,
    )
    assert '切换服务器后已取消' in js
