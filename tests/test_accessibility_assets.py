"""Focused contracts for keyboard-operable dynamic WebView controls."""
from __future__ import annotations

import re
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"


def _source(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def test_multiselect_disclosure_tracks_state_and_escape_returns_focus():
    source = _source("ui.js")
    multiselect = _block(source, "function multiselect(host, opts)", "// ── 3)")

    assert "btn.setAttribute('aria-controls', pop.id)" in multiselect
    assert "btn.setAttribute('aria-expanded', 'false')" in multiselect
    assert "pop.setAttribute('aria-labelledby', btn.id)" in multiselect
    assert "tr('multiselect.remove', '移除 {label}', { label })" in multiselect
    assert "'\" aria-label=\"' + esc(remove) + '\">'" in multiselect
    assert "if (e.key !== 'Escape' || pop.hidden) return" in multiselect
    assert "setPopOpen(pop, false, true)" in multiselect
    assert "button.setAttribute('aria-expanded', open ? 'true' : 'false')" in source
    assert "if (!open && returnFocus) button.focus()" in source


def test_segmented_buttons_expose_selection_and_support_roving_arrow_keys():
    source = _source("ui.js")
    segmented = _block(source, "function enhanceSegmented(root)", "function enhanceAll(root)")

    assert "seg.setAttribute('role', 'group')" in segmented
    assert "b.setAttribute('aria-pressed', selected ? 'true' : 'false')" in segmented
    assert "b.setAttribute('tabindex', selected ? '0' : '-1')" in segmented
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in segmented
    assert "next.click()" in segmented
    assert "next.focus()" in segmented


def test_dashboard_click_surfaces_have_equivalent_link_keyboard_semantics():
    source = _source("dashboard.js")
    assert '<div class="db-num ${cls}" data-goto="jobs" role="link" tabindex="0"' in source
    assert '<div class="db-row" data-goto="jobs" role="link" tabindex="0"' in source
    assert "e.target.closest('[data-goto][role=\"link\"]')" in source
    assert "if (!el || e.key !== 'Enter') return" in source
    assert "navTo(el.dataset.goto)" in source


def test_job_groups_use_real_disclosure_buttons_and_restore_focus_after_render():
    source = _source("jobs.js")
    grouping = _block(source, "function groupHeadHtml(", "function renderTable()")
    binding = _block(source, "function bindTable()", "// ── 远程动作")

    assert '<button type="button" class="btn quiet grp-toggle"' in grouping
    assert 'aria-expanded="${open ? \'true\' : \'false\'}"' in grouping
    assert 'aria-controls="${groupBodyId(key)}"' in grouping
    assert 'id="${groupBodyId(key)}" class="job-group-body"' in source
    assert "toggleGroup(toggle.dataset.groupToggle, true)" in binding
    assert "toggleGroup(gh.dataset.grp)" not in binding
    assert "button.focus({ preventScroll: true })" in source


def test_dynamic_job_tables_and_scroll_regions_are_named_and_focusable():
    source = _source("jobs.js")

    assert "runtime.jobs.a11y.table_scroll" in source
    assert "runtime.jobs.a11y.remote_files_scroll" in source
    assert "region.setAttribute('tabindex', '0')" in source
    assert "runtime.jobs.a11y.select_named_job" in source
    assert "runtime.jobs.group.toggle_aria" in source
    assert "runtime.jobs.queue.table_label" in source
    assert 'role="region" aria-label="${VCS.esc(queueLabel)}"' in source
    assert 'tabindex="0" style="max-height:52vh;overflow:auto"' in source
    assert '<table aria-label="${VCS.esc(queueLabel)}">' in source
    assert '<table class="fm-tbl" aria-label="${VCS.esc(tr(' in source
    assert "runtime.jobs.files.select_named" in source
    assert source.count('scope="col"') >= 10


def test_modal_stack_inerts_background_and_only_top_dialog_handles_keyboard():
    source = _source("app.js")
    modal = _block(source, "modal({ title, bodyHTML", "// 确认框")

    assert "const vcsModalStack = []" in source
    assert "vcsModalStack[vcsModalStack.length - 1]" in source
    assert "document.addEventListener('keydown', vcsModalKeydown, true)" in source
    assert "element.setAttribute('inert', '')" in source
    assert "element.setAttribute('aria-hidden', 'true')" in source
    assert "vcsModalBackground.forEach" in source
    assert "modal title is required for an accessible dialog" in modal
    assert "card.setAttribute('aria-labelledby', titleId)" in modal
    assert "b.type = 'button'" in modal
    assert "handle.dismiss" in modal
    assert "document.addEventListener('keydown', onKey)" not in modal


def test_programmatic_accordion_opening_updates_disclosure_state():
    ui = _source('ui.js')
    assert 'function setAccordionOpen(target, value, persist = false)' in ui
    assert "toggle.setAttribute('aria-expanded', open ? 'true' : 'false')" in ui
    assert 'enhanceAccordion, enhanceSwitchers, enhanceSegmented, setAccordionOpen' in ui
    for name in ('app.js', 'generate.js', 'taskcat.js', 'workspace.js'):
        assert 'VCS.ui.setAccordionOpen' in _source(name), name


def test_modal_drawers_filter_hidden_descendants_and_isolate_background():
    workspace = _source('workspace.js')
    assert "el.closest('[hidden],[inert],[aria-hidden=\"true\"],fieldset[disabled]')" in workspace
    assert "style.display === 'none' || style.visibility === 'hidden'" in workspace
    assert 'el.getClientRects().length > 0' in workspace
    assert 'function isolateDrawer(drawer, backdrop)' in workspace
    assert "element.setAttribute('inert', '')" in workspace
    assert "element.setAttribute('aria-hidden', 'true')" in workspace
    assert workspace.count('restoreDrawerIsolation();') >= 3
    assert "drawer.setAttribute('aria-modal', 'false')" in workspace


def test_password_and_first_launch_controls_use_native_accessible_names():
    source = _source("app.js")

    assert 'for="pw" data-i18n="cluster.password.label"' in source
    assert 'data-i18n-ph="cluster.password.placeholder"' in source
    assert '<button type="button" class="scene-card"' in source
    assert '<div class="scene-card"' not in source
