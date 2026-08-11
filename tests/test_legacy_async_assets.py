"""Phase B 旧业务页与工作区恢复之间的异步接缝合同。"""
from __future__ import annotations

from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' / 'assets'


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding='utf-8')


def _block(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


def test_project_reload_is_last_response_wins_before_any_state_or_dom_commit():
    source = _source('project.js')
    perform = _block(
        source,
        'async function performProjectReload(',
        'async function reloadProjects(',
    )
    guard = 'generation !== State.projectReloadGeneration'
    assert guard in perform
    assert perform.index(guard) < perform.index('State.projects =')
    reload_block = _block(
        source,
        'async function reloadProjects(',
        '// ── 论文级出图',
    )
    assert 'const generation = ++State.projectReloadGeneration' in reload_block
    assert 'State.projectReloadInFlight = { generation, promise: pending }' in reload_block
    assert 'while (result && result.stale)' in reload_block


def test_project_explicit_selection_survives_eager_reload_and_stale_selector_loses():
    source = _source('project.js')
    perform = _block(
        source,
        'async function performProjectReload(',
        'async function reloadProjects(',
    )
    assert 'const requested = State.requestedProject' in perform
    assert perform.index('requestedHit && projectId(requestedHit)') < perform.index(
        'workspaceId, liveSelection, active && projectId(active)'
    )
    selector = _block(
        source,
        'async function selectRequestedProject(',
        'async function selectById(',
    )
    assert 'const generation = ++State.projectSelectionGeneration' in selector
    assert 'State.requestedProject = request' in selector
    assert selector.index('await reloadProjects(') < selector.index(
        'generation !== State.projectSelectionGeneration'
    ) < selector.index('applyProjectSelection(hit)')
    assert 'return selectRequestedProject(id)' in source
    assert 'selectByPath' not in source


def test_native_project_switch_uses_same_generation_and_returns_stale_apply_signal():
    source = _source('project.js')
    switch = _block(
        source,
        'async function requestProjectSelection(',
        'function matchesRequestedProject(',
    )
    assert 'const selectionGeneration = ++State.projectSelectionGeneration' in switch
    assert 'State.requestedProject = null' in switch
    assert 'selectionGeneration !== State.projectSelectionGeneration' in switch
    assert 'return false' in switch
    assert switch.index('selectionGeneration !== State.projectSelectionGeneration') < switch.index(
        'applyProjectSelection(project)'
    )


def test_jobs_reload_commits_profiles_rows_and_filters_only_for_latest_generation():
    source = _source('jobs.js')
    perform = _block(source, 'async function performReload(', 'async function reload()')
    guard = 'generation !== State.reloadGeneration'
    assert guard in perform
    assert perform.index(guard) < perform.index('applyProfiles(profiles)')
    assert perform.index(guard) < perform.index('State.rows =')
    assert perform.index('State.rows =') < perform.index('refreshClusterFilter()')
    reload_block = _block(source, 'async function reload()', '// 成员角色')
    assert 'const generation = ++State.reloadGeneration' in reload_block
    assert 'State.reloadInFlight = { generation, promise: pending }' in reload_block
    assert 'while (result && result.stale)' in reload_block


def test_jobs_dynamic_cluster_filter_is_restored_from_current_workspace_after_options_exist():
    source = _source('jobs.js')
    helper = _block(source, 'function workspaceJobControlValue(', 'function refreshClusterFilter(')
    assert 'VCS.workspace' in helper
    assert 'workspace.current' in helper and 'workspace.state' in helper
    refresh = _block(source, 'function refreshClusterFilter(', '// 组头折叠开关')
    assert "workspaceJobControlValue('cluster', 'job_cluster')" in refresh
    assert '...Object.keys(State.profiles)' in refresh
    assert 'if (wanted && clusters.indexOf(wanted) < 0) clusters.push(wanted)' in refresh
    assert refresh.index('sel.innerHTML =') < refresh.index('sel.value = wanted')
    assert "dispatchEvent(new Event('change', { bubbles: true }))" in refresh


def test_stale_async_job_selection_cannot_publish_over_newer_context():
    source = _source('jobs.js')
    created = _block(source, 'async function selectCreatedProject(', 'async function selectById(')
    assert 'const selectionGeneration = ++State.selectionGeneration' in created
    assert created.index('await reload()') < created.index(
        'selectionGeneration !== State.selectionGeneration'
    ) < created.index('State.selected.add')
    by_id = _block(source, 'async function selectById(', '// 供集群页保存')
    assert 'const selectionGeneration = ++State.selectionGeneration' in by_id
    assert by_id.index('await reload()') < by_id.index(
        'selectionGeneration !== State.selectionGeneration'
    ) < by_id.index('publishJobContext(row)')
    assert by_id.index('jobBelongsToWorkspaceProject(row)') < by_id.index(
        'State.selected.clear()'
    )
    assert by_id.index('revealSelectedJob(row)') < by_id.index('State.selected.clear()')
    assert by_id.index('publishJobContextCleared()') < by_id.index('State.selected.clear()')
    page_events = _block(source, '// 切回作业页时刷新台账',
                         "if (document.readyState === 'loading')")
    assert "if (e.detail.page === 'jobs') reload()" in page_events
    assert 'else State.selectionGeneration += 1' in page_events
    assert "document.addEventListener('vcs:workspace-project'" in page_events
    assert "document.addEventListener('vcs:project-context'" in page_events
    assert 'State.selected.clear()' in page_events
    assert 'publishJobContextCleared()' in page_events


def test_jobs_publish_clear_context_when_the_last_selected_row_is_removed():
    source = _source('jobs.js')
    clear = _block(source, 'function publishJobContextCleared()',
                   'function publishCurrentJobSelection(')
    assert "detail: { id: null, job_id: null, clear: true }" in clear
    publish = _block(source, 'function publishCurrentJobSelection(',
                     'function currentWorkspaceProjectId(')
    assert 'if (row) return publishJobContext(row)' in publish
    assert 'publishJobContextCleared()' in publish
    table = _block(source, 'function renderTable()', '// ── 筛选')
    assert 'const hadSelection = State.selected.size > 0' in table
    assert 'if (hadSelection && !State.selected.size) publishJobContextCleared()' in table


def test_manual_job_context_always_prefers_canonical_project_id():
    source = _source('jobs.js')
    publish = _block(source, 'function publishJobContext(row)',
                     'function publishJobContextCleared()')
    assert "row.project_id || ''" in publish
    assert 'project_id: projectId' in publish
    assert 'project_uuid' not in publish


def test_jobs_project_jump_selects_atomically_before_committing_project_deep_link():
    source = _source('jobs.js')
    goto = _block(source, 'async function gotoProject(', '// 「导出报告」')
    modern = goto[goto.index('if (workspace && typeof workspace.requestProjectSwitch'):
                  goto.index('const selected = await window.Project.selectById')]
    assert 'workspace.requestProjectSwitch(targetId' in modern
    assert 'window.Project.selectById(targetId)' in modern
    assert "workspace.navigateRoute('project-overview', {" in modern
    assert 'projectId: targetId' in modern
    assert modern.index('workspace.requestProjectSwitch(') < modern.index(
        "workspace.navigateRoute('project-overview'"
    )
    assert "VCS.navigate('project'" not in modern

    grouping = _block(source, 'function groupHeadHtml(', 'function renderTable()')
    assert 'data-project-id=' in grouping
    assert 'data-proj-path=' not in grouping
    report = _block(source, 'async function doReport()', 'function renderStats()')
    assert "projectId = String(row.project_id || '').trim()" in report
    assert 'gotoProject(projectId)' in report


def test_jobs_exposes_deterministic_cross_project_selection_rollback():
    source = _source('jobs.js')
    clear = _block(source, 'function clearSelection()', '// 供集群页保存')
    assert 'State.selectionGeneration += 1' in clear
    assert clear.index('State.selected.clear()') < clear.index('renderTable()')
    assert clear.index('renderTable()') < clear.index('publishJobContextCleared()')
    assert 'return hadSelection' in clear
    assert (
        'const publicJobs = { reload, selectCreatedProject, selectById, clearSelection }'
        in source
    )
    assert 'window.Jobs = publicJobs' in source
