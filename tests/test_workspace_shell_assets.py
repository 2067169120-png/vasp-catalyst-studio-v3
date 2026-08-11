"""Phase B workspace shell 的前端资产合同。

这些测试不执行 WebView，只锁定壳层与旧业务页之间必须稳定的接缝。
JavaScript 语法由聚焦测试中的 ``node --check`` 覆盖。
"""
from __future__ import annotations

import re
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' / 'assets'


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding='utf-8')


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', tag)
    return match.group(1) if match else ''


def test_primary_shell_has_exactly_seven_semantic_areas_and_real_hash_links():
    html = _source('index.html')
    start = html.find('<div class="shell-primary-nav"')
    end = html.find('<div class="shell-nav-footer"', start)
    assert start >= 0 and end > start, '找不到一级 workspace 导航'
    block = html[start:end]
    tags = re.findall(r'<a\b[^>]*\bdata-route="[^"]+"[^>]*>', block)
    assert [_attr(tag, 'data-area') for tag in tags] == [
        'home', 'project', 'prepare', 'run', 'analyze', 'publish', 'environment',
    ]
    assert [_attr(tag, 'data-route') for tag in tags] == [
        'home', 'project-overview', 'prepare-structure', 'run-jobs',
        'analyze-energy', 'publish-figures', 'environment-settings',
    ]
    assert all(_attr(tag, 'href').startswith('#/') for tag in tags)
    assert all(_attr(tag, 'data-scene').startswith('pages.') for tag in tags)
    assert 'id="nav-ai"' not in block


def test_workspace_router_loads_before_legacy_pages_and_keeps_legacy_navigation():
    html = _source('index.html')
    app = _source('app.js')
    workspace = _source('workspace.js')
    assert html.index('<script src="app.js"></script>') < html.index(
        '<script src="workspace.js"></script>') < html.index('<script src="jobs.js"></script>')
    assert "window.addEventListener('hashchange'" in workspace
    assert 'history.pushState(' in workspace and 'history.replaceState(' in workspace
    assert 'function parseRoute(' in workspace and 'function routeHash(' in workspace
    assert 'async function navigateLegacy(page, options = {})' in workspace
    assert 'VCS.navigate = async function (page, options = {})' in app
    assert 'VCS.workspace.navigateLegacy(page, options)' in app


def test_shell_removes_global_step_numbers_and_ai_is_a_context_drawer():
    html = _source('index.html')
    css = _source('app.css')
    assert not re.search(r'class="[^"]*\bstepno\b', html)
    assert '.stepno' not in css
    assert re.search(
        r'id="assistant-toggle"[^>]*data-scene="pages\.ai"[^>]*'
        r'aria-controls="page-ai"[^>]*aria-expanded="false"', html)
    assert re.search(
        r'<section\b[^>]*id="page-ai"[^>]*data-shell-assistant[^>]*'
        r'role="dialog"[^>]*aria-labelledby="assistant-title"', html, re.S)
    assert 'id="assistant-backdrop"' in html
    assert '#page-ai[data-shell-assistant]' in css


def test_assistant_drawer_is_context_first_and_separates_advanced_tools():
    html = _source('index.html')
    workspace = _source('workspace.js')
    context = html.index('id="assistant-context-heading"')
    chat = html.index('id="ai-chat-card"')
    advanced = html.index('id="assistant-advanced-tools"')
    assert context < chat < advanced
    assert 'id="assistant-context-project"' in html
    assert 'id="assistant-context-task"' in html
    assert 'id="assistant-context-stage"' in html
    assert 'id="assistant-context-report"' in html
    assert html.count('data-assistant-route=') == 3
    assert '<details class="assistant-advanced"' in html
    assert 'function renderAssistantContext()' in workspace
    assert "selectedProject.scientific_status || selectedProject.artifact_status" in workspace
    assert "source: 'assistant-context'" in workspace


def test_compact_action_bar_does_not_bias_dashboard_and_exposes_one_recommendation():
    workspace = _source('workspace.js')
    compact = re.search(
        r'function renderCompactActions\(\) \{(.*?)\n  \}', workspace, re.S,
    ).group(1)
    assert "section.dataset.page === 'dashboard'" in compact
    assert "section.querySelectorAll('[data-compact-primary]:not([disabled])')" in compact
    assert '}).slice(0, 1);' in compact


def test_responsive_navigation_dirty_guard_and_activity_center_are_first_class():
    html = _source('index.html')
    css = _source('app.css')
    workspace = _source('workspace.js')
    assert 'id="shell-nav" aria-label="主导航"' in html
    assert re.search(
        r'id="nav-toggle"[^>]*aria-label="打开主导航"[^>]*'
        r'aria-controls="shell-nav"[^>]*aria-expanded="false"', html, re.S)
    assert '@media (max-width:959px)' in css
    assert '#shell-nav.open' in css and '.compact-actions' in css
    assert 'id="workspace-dirty" role="status"' in html
    assert 'VCS.unsaved = {' in workspace
    assert "window.addEventListener('beforeunload'" in workspace
    assert 'async function requestProjectSwitch(projectId, apply)' in workspace
    assert "nav.setAttribute('inert', '')" in workspace
    assert "nav.setAttribute('aria-hidden', 'true')" in workspace
    assert "nav.removeAttribute('inert')" in workspace
    assert re.search(
        r'<aside\b[^>]*id="activity-drawer"[^>]*role="dialog"[^>]*aria-modal="true"',
        html, re.S)
    assert 'id="activity-list" role="feed" aria-live="polite"' in html
    assert 'const operationRecords = new Map();' in workspace
    assert 'VCS.operations = {' in workspace
    assert 'publish: publishOperation' in workspace
    assert "data-operation-route=" in workspace
    assert "source: 'operation-queue'" in workspace


def test_dashboard_delegates_navigation_without_clicking_a_sidebar_link():
    dashboard = _source('dashboard.js')
    assert 'return VCS.navigate(page,' in dashboard
    assert "querySelector('nav a[data-page=" not in dashboard
    assert '.click();' not in re.search(
        r'function navTo\(page, options = \{\}\) \{(.*?)\n  \}', dashboard, re.S
    ).group(1)


def test_dashboard_resume_center_uses_workspace_draft_refs_without_exposing_bodies():
    html = _source('index.html')
    dashboard = _source('dashboard.js')
    assert 'id="db-resume-card"' in html and 'id="db-resume" aria-live="polite"' in html
    assert 'function renderResumeCenter()' in dashboard
    assert 'state.draft_refs' in dashboard
    assert "VCS.unsaved.scopes()" in dashboard
    assert "VCS.workspace.parseRoute(resume.dataset.resumeRoute)" in dashboard
    assert "source: 'resume-center'" in dashboard
    resume = dashboard[dashboard.index('function renderResumeCenter()'):
                       dashboard.index('// 取数出错')]
    assert '.text' not in resume and 'localStorage' not in resume


def test_project_selection_uses_workspace_guard_and_publishes_stable_context():
    project = _source('project.js')
    assert "new CustomEvent('vcs:project-context', { detail })" in project
    context = project[project.index('function projectContext('):
                      project.index('function publishProjectContext(')]
    for field in ('project_id:', 'name:', 'counts:'):
        assert field in project
    for locator in ('project_uuid:', 'path:', 'project_path:', 'locator:'):
        assert locator not in context
    assert 'VCS.workspace.requestProjectSwitch(projectId(project), apply)' in project
    assert 'localStorage.removeItem(LEGACY_CURRENT_PROJECT_KEY)' in project
    assert 'localStorage.removeItem(LEGACY_COMPARE_PROJECTS_KEY)' in project
    assert 'localStorage.setItem(LEGACY_CURRENT_PROJECT_KEY' not in project
    assert 'location.hash' not in project and 'history.pushState' not in project
    assert 'selectById,' in project and 'current,' in project and 'list,' in project
    assert 'selectByPath' not in project


def test_jobs_table_is_a_focusable_scroll_region_and_publishes_stable_job_context():
    html = _source('index.html')
    jobs = _source('jobs.js')
    assert re.search(
        r'id="jobs-card"[^>]*role="region"[^>]*aria-label="作业列表"[^>]*tabindex="0"',
        html)
    assert "card.classList.add('table-scroll', 'jobs-table-scroll-region')" in jobs
    assert "card.setAttribute('role', 'region')" in jobs
    assert "card.setAttribute('tabindex', '0')" in jobs
    assert "new CustomEvent('vcs:job-context'" in jobs
    for field in ('id,', 'dir:', 'name:', 'state:', 'project_id:'):
        assert field in jobs
    assert 'async function selectById(id)' in jobs
    assert 'const publicJobs = { reload, selectCreatedProject, selectById, clearSelection }' in jobs
    assert 'window.Jobs = publicJobs' in jobs
    assert "return String(row.dir" not in re.search(
        r'function stableJobId\(row\) \{(.*?)\n  \}', jobs, re.S
    ).group(1)


def test_router_commits_history_only_after_route_preflight_and_application():
    workspace = _source('workspace.js')
    navigate = re.search(
        r'async function performNavigateRoute\(id, options = \{\}\) \{(.*?)\n  \}',
        workspace, re.S,
    ).group(1)
    assert 'routeCanApply(route)' in navigate
    assert navigate.index('const out = await applyRoute(') < navigate.index(
        'history.pushState(')
    assert "VCS.canActivatePage(def.page)" in workspace
    assert 'if (!selected) return { ok: false' in workspace


def test_explicit_route_focus_runs_after_next_frame_and_is_not_stolen_by_heading():
    workspace = _source('workspace.js')
    apply_now = re.search(
        r'async function applyRouteNow\(route, options = \{\}\) \{(.*?)\n  \}',
        workspace, re.S,
    ).group(1)
    frame = apply_now.index('await new Promise(resolve => requestAnimationFrame(')
    explicit = apply_now.index('const focused = await VCS.focusNavigationTarget(')
    assert frame < explicit
    assert 'if (!hasExplicitFocus) focusPageHeading(' in apply_now
    assert 'generation !== routeGeneration' in apply_now[frame:explicit]


def test_history_events_share_one_dirty_guarded_external_navigation_path():
    workspace = _source('workspace.js')
    assert re.search(
        r"window\.addEventListener\('popstate', event => "
        r"handleExternalNavigation\('popstate', event\)\)", workspace)
    assert re.search(
        r"window\.addEventListener\('hashchange', event => "
        r"handleExternalNavigation\('hashchange', event\)\)", workspace)
    handler = re.search(
        r'function handleExternalNavigation\(source, event\) \{(.*?)\n  \}', workspace, re.S
    ).group(1)
    assert "await guardUnsaved('返回到其他页面')" in handler
    assert 'restoreExternalLocation(source, targetIndex)' in handler
    assert "history.pushState(" not in handler
    assert 'history.go(historyIndex - targetIndex)' in workspace
    assert 'vcsIndex: historyIndex' in workspace


def test_initial_history_index_handles_a_null_browser_state():
    workspace = _source('workspace.js')
    assert "const initialHistoryState = history.state && typeof history.state === 'object'" in workspace
    assert 'let historyIndex = initialHistoryState &&' in workspace
    assert 'Number(initialHistoryState.vcsIndex)' in workspace
    assert 'Number(history.state.vcsIndex)' not in workspace


def test_job_route_rejects_unknown_status_instead_of_showing_all_jobs():
    workspace = _source('workspace.js')
    assert "const JOB_STATUS_FILTERS = new Set(['queue', 'run', 'need', 'done', 'fail']);" in workspace
    assert "return JOB_STATUS_FILTERS.has(status) ? status : '';" in workspace
    assert "if (name !== 'cluster') return safeToken(value, JOB_TOKEN);" in workspace
    assert "out.length > 128" in workspace
    assert "[\\x00-\\x1f\\x7f\\\\/?#&=]" in workspace


def test_remote_workspace_revision_never_pairs_with_a_stale_local_snapshot():
    workspace = _source('workspace.js')
    assert 'server_revision: 0' in workspace
    assert 'localBaseRevision !== serverRevision' in workspace
    assert 'if (shouldAdoptRemote)' in workspace
    assert 'state = remote;' in workspace
    assert workspace.index('state = remote;') < workspace.index(
        'state.server_revision = serverRevision;', workspace.index('state = remote;'))
    assert 'remoteSaveBlocked = true;' in workspace
    assert 'setTimeout(() => refreshWorkspaceContext(), 0);' in workspace


def test_remote_saves_and_context_refreshes_are_serialized_last_wins():
    workspace = _source('workspace.js')
    assert 'let remoteSaveInFlight = null;' in workspace
    assert 'while (remoteSaveRequested && !remoteSaveBlocked)' in workspace
    assert 'const savedGeneration = localStateGeneration;' in workspace
    assert 'localStateGeneration !== savedGeneration' in workspace
    assert 'const refreshGeneration = ++contextRefreshGeneration;' in workspace
    assert workspace.count('refreshGeneration !== contextRefreshGeneration') >= 3
    assert 'if (serverRevision < revision) return;' in workspace
    assert 'let adoptedRemoteRoute = false;' in workspace
    assert 'const selectionRoute = adoptedRemoteRoute ? parseRoute(state.route) : currentRoute;' in workspace


def test_queued_navigation_is_invalidated_by_new_browser_location_intent():
    workspace = _source('workspace.js')
    navigate = re.search(
        r'function navigateRoute\(id, options = \{\}\) \{(.*?)\n  \}', workspace, re.S
    ).group(1)
    assert 'const locationIntent = locationIntentGeneration;' in navigate
    assert 'Object.assign({}, options, { locationIntent })' in navigate
    perform = re.search(
        r'async function performNavigateRoute\(id, options = \{\}\) \{(.*?)\n  \}',
        workspace, re.S,
    ).group(1)
    assert perform.count('locationIntent !== locationIntentGeneration') >= 2
    assert 'history.go(historyIndex - targetIndex)' in workspace
    assert 'history.back();' in workspace


def test_draft_projection_and_job_ids_are_bounded_without_blocking_state_save():
    workspace = _source('workspace.js')
    assert 'const JOB_TOKEN = /^[A-Za-z0-9._~:-]{1,160}$/;' in workspace
    assert 'safeToken(src.selected_job_id, JOB_TOKEN)' in workspace
    assert "safeToken(detail.id || detail.job_id || detail.job_uuid, JOB_TOKEN)" in workspace
    payload = re.search(r'function statePayload\(\) \{(.*?)\n  \}', workspace, re.S).group(1)
    assert 'Number.isFinite(updatedAtMs)' in payload
    assert "Date.parse(String(ref.updated_at || ''))" in payload
    assert 'Math.min(MAX_DRAFT_CHARS' in payload
    assert "new Date(Number(ref.updated_at_ms" not in payload


def test_compact_actions_exclude_hidden_and_collapsed_controls_and_refresh():
    workspace = _source('workspace.js')
    compact = re.search(
        r'function renderCompactActions\(\) \{(.*?)\n  \}', workspace, re.S
    ).group(1)
    for marker in (
        '[hidden]', '[data-scene-hidden]', '[data-engine-hidden]',
        '[data-task-hidden]', '.acc[data-open="0"]',
    ):
        assert marker in compact
    assert "'data-open'" in workspace
    assert "'data-scene-hidden'" in workspace


def test_semantic_routes_select_the_registry_driven_analysis_workbench():
    workspace = _source('workspace.js')
    routes = workspace.split('const ROUTES = Object.freeze({', 1)[1].split(
        'const AREA_LABELS', 1)[0]
    comparison = routes.split("'analyze-comparison':", 1)[1].split(
        "'analyze-custom':", 1)[0]
    report = routes.split("'publish-report':", 1)[1].split("'publish-si':", 1)[0]
    draft = routes.split("'publish-draftpack':", 1)[1].split(
        "'publish-versions':", 1)[0]
    assert "page: 'analysis-workbench'" in comparison
    assert "analysisId: 'multi-project-comparison'" in comparison
    assert "scenePage: 'project'" in comparison
    assert "focus: '#aw-title'" in comparison
    for analysis_id in (
        'adsorption-energy', 'free-energy-path', 'electronic-structure',
        'charge-wavefunction', 'multi-project-comparison', 'task-results',
    ):
        assert f"analysisId: '{analysis_id}'" in routes
    assert "page: 'report-workbench'" in report and "focus: '#rw-title'" in report
    assert "page: 'report-workbench'" in draft and "focus: '#rw-step-button-export'" in draft
    assert 'function syncPrimaryAreaRoutes(route)' in workspace


def test_project_and_job_context_switches_are_atomic_and_fail_closed():
    workspace = _source('workspace.js')
    assert 'let projectSwitchGeneration = 0;' in workspace
    switch = re.search(
        r'async function requestProjectSwitch\(projectId, apply\) \{(.*?)\n  \}',
        workspace, re.S,
    ).group(1)
    assert 'applied === false || generation !== projectSwitchGeneration' in switch
    assert "new CustomEvent('vcs:workspace-project'" in switch
    assert "typeof window.Jobs.clearSelection === 'function'" in workspace
    assert 'const jobProjectId = safeToken(detail.project_id, PROJECT_TOKEN);' in workspace
    assert 'await requestProjectSwitch(jobProjectId' in workspace
    selector = workspace.split("const project = document.getElementById('workspace-project');", 1)[1]
    selector = selector.split("const nav = document.getElementById('shell-nav');", 1)[0]
    assert 'return window.Project.selectById(hit.id);' in selector
    assert 'selectByPath' not in selector


def test_unsaved_scopes_run_component_discard_before_shell_clear():
    workspace = _source('workspace.js')
    guard = workspace.split('async function guardUnsaved(reason)', 1)[1].split(
        'function findPrimaryRoute', 1)[0]
    assert "typeof item.canDiscard === 'function'" in guard
    assert "typeof item.discard === 'function'" in guard
    assert guard.index('await item.discard()') < guard.index('dirtyScopes.clear()')
    assert "mark(scope, label = '当前编辑', lifecycle = null)" in workspace
