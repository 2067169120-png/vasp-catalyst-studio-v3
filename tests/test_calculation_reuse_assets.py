"""Web assets expose server evidence without calculating scientific identity."""
from pathlib import Path
import shutil
import subprocess

import pytest


ASSETS = Path(__file__).resolve().parents[1] / "vcstudio" / "gui_web" / "assets"


def _source(name):
    return (ASSETS / name).read_text(encoding="utf-8")


def _between(source, start, end):
    return source[source.index(start):source.index(end, source.index(start))]


def test_jobs_page_has_explainable_reuse_panel_and_explicit_controls():
    html = _source("index.html")
    css = _source("app.css")

    for identifier in (
            'id="jobs-reuse-advisory"', 'id="jobs-reuse-advisory-out"',
            'id="jobs-reuse-reason"', 'id="jb-tray-reuse-scan"'):
        assert identifier in html
    assert "引用既有结果" in _source("jobs.js")
    assert ".jobs-reuse-match.exact" in css
    assert ".jobs-reuse-match.near" in css
    assert ".jobs-reuse-match.bad" in css


def test_browser_queries_opaque_ids_and_does_not_compute_fingerprints_or_equivalence():
    script = _source("jobs.js")
    query = _between(script, "async function inspectSelectedReuse", "async function referenceExistingResult")
    renderer = _between(script, "function reuseMatchHtml", "function renderReuseAdvisory")

    assert "rows.map(stableJobId)" in query
    assert "VCS.call('jobs_reuse_advisory', ids)" in query
    for forbidden in (
            "row.dir", "POSCAR", "INCAR", "KPOINTS", "POTCAR", "FileReader",
            "crypto.subtle", "parseFloat", "parseInt", "Math."):
        assert forbidden not in query
    assert "verification.reusable === true" in renderer
    assert "near match 不代表等价" in renderer
    assert "not an equivalence claim" in renderer
    assert "VCS.call('submit_jobs'" not in query
    assert "VCS.call('jobs_reference_existing_result'" not in query


def test_only_explicit_actions_reference_or_force_and_advisory_precedes_submit():
    script = _source("jobs.js")
    reference = _between(script, "async function referenceExistingResult", "function resourceRiskLabel")
    submit = _between(script, "async function doSubmit", "async function runStatus")

    assert "VCS.confirm" in reference
    assert "VCS.call('jobs_reference_existing_result'" in reference
    assert "accepted/final" in reference
    assert "await inspectSelectedReuse(true)" in submit
    assert "VCS.call('jobs_force_recalculation'" in submit
    assert "force_reason_required" in submit
    assert submit.index("await inspectSelectedReuse(true)") < submit.index("VCS.call('submit_jobs'")


def test_project_one_stop_reuses_one_operation_id_across_password_and_trust_retries():
    script = _source("project.js")
    function = _between(script, "async function submitLiSProject", "function preparedProjectId")

    assert "const operationId = 'project-submit-'" in function
    assert "password, trust, operationId" in function
    assert function.count("VCS.call('submit_project_with_resources'") == 1


@pytest.mark.parametrize("name", ["jobs.js", "project.js"])
def test_modified_production_scripts_parse_in_node(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    subprocess.run([node, "--check", str(ASSETS / name)], check=True)
