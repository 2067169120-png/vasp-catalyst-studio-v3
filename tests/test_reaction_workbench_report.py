from __future__ import annotations

import copy
import json
from pathlib import Path

from tests.test_reaction_workbench import projection
from tests.test_reaction_workbench_api import _DomainSource
from tests.test_report_workbench import (
    _html_request,
    _project_id,
    _publish,
    _workbench_api,
)


def test_reaction_binding_enters_snapshot_validation_model_and_html(tmp_path):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    api._reaction_domain_source = _DomainSource(projection())
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    request = _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=[
            "executive_summary", "adsorption_table", "reaction_map_table",
            "thermochemistry_table", "figures", "methods", "limitations",
        ],
    )

    preview = api.report_workbench_preview(project_id, request)

    assert preview["ok"] is True
    snapshot = preview["public_snapshot"]
    reaction = snapshot["payload"]["reaction_workbench"]
    assert reaction["graph"]["schema"] == "vcstudio.reaction-graph/v1"
    assert reaction["ledger"]["schema"] == "vcstudio.thermochemistry-ledger/v1"
    assert reaction["condition_revision"]["schema"] == (
        "vcstudio.condition-derived-revision/v1")
    assert reaction["frozen_network"]["schema"] == (
        "vcstudio.frozen-reaction-network/v1")
    assert reaction["report_binding"]["schema"] == (
        "vcstudio.reaction-report-binding/v1")
    assert any(source["kind"] == "canonical_reaction_domain_projection"
               for source in snapshot["sources"])
    evidence = snapshot["evidence"]["reaction_binding"]
    assert evidence["source_projection_sha256"] == reaction["graph"][
        "source_projection_sha256"]
    check_ids = {item["id"] for item in preview["validation"]["checks"]}
    assert {"canonical-reaction-binding", "reaction-kinetic-readiness"} <= check_ids
    assert "反应图边" in preview["html"]
    assert "热化学账本" in preview["html"]
    assert "data:image/png;base64," in preview["html"]
    assert "证据绑定反应图" in preview["html"] or "Evidence-bound reaction map" in preview["html"]
    encoded = json.dumps(preview, ensure_ascii=False)
    assert str(tmp_path) not in encoded
    assert preview["scientific_qualification"] == "adsorption_result_verified"

    published = _publish(api, project_path, tmp_path / "published", preview)
    assert published["ok"] is True
    assert published["artifact_status"] == "complete"
    status = api.proj_report_status(project_id)
    assert status["artifact_current"] is True


def test_reaction_binding_change_invalidates_report_preview_cas(tmp_path):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    source = _DomainSource(projection())
    api._reaction_domain_source = source
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    request = _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=[
            "executive_summary", "reaction_map_table", "thermochemistry_table",
            "figures", "methods", "limitations",
        ],
    )
    preview = api.report_workbench_preview(project_id, request)
    assert preview["ok"] is True
    preview_record = api._reports()._previews[preview["preview_id"]]

    changed = copy.deepcopy(source.value)
    changed["bindings"]["state-p"]["thermochemistry"][
        "electronic_energy_e0_eV"]["value"] -= 0.2
    source.value = changed
    current = api._report_workbench_current_state(
        project_path, preview["report_spec"])

    assert current["scientific_fingerprint"] != preview_record.build[
        "scientific_fingerprint"]


def test_verified_ts_evidence_can_raise_only_bounded_kinetic_qualification(tmp_path):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    value = projection()
    for binding in value["bindings"].values():
        binding["scientific_status"] = "verified"
    api._reaction_domain_source = _DomainSource(value)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    preview = api.report_workbench_preview(project_id, _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=[
            "executive_summary", "reaction_map_table", "thermochemistry_table",
            "figures", "methods", "limitations",
        ],
    ))

    assert preview["ok"] is True
    assert preview["scientific_qualification"] == "kinetic_evidence_verified"
    assert preview["validation"]["claim_ceiling"] == (
        "bound_elementary_step_kinetics_not_complete_mechanism")
    claims = {item["id"]: item for item in preview["validation"]["claims"]}
    assert claims["claim.kinetic.bound-steps"]["status"] == "supported"
    assert "不证明机理完整" in claims["claim.kinetic.bound-steps"]["text"]
    graph = preview["public_snapshot"]["payload"]["reaction_workbench"]["graph"]
    assert graph["mechanism_complete"] is False


def test_untyped_condition_response_fields_never_enter_frozen_snapshot(tmp_path):
    api, project_path, state, _manifests = _workbench_api(tmp_path)
    value = projection()
    response = value["bindings"]["state-r"]["thermochemistry"][
        "condition_response"]
    response["operator_note"] = r"C:\private\reaction-notes"
    response["auth_blob"] = "token=not-for-derived-artifacts"
    api._reaction_domain_source = _DomainSource(value)
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    preview = api.report_workbench_preview(project_id, _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=[
            "executive_summary", "reaction_map_table", "thermochemistry_table",
            "figures", "methods", "limitations",
        ],
    ))

    assert preview["ok"] is True
    published = _publish(api, project_path, tmp_path / "safe-published", preview)
    assert published["ok"] is True
    snapshot_path = state["project"]["autopilot_report"]["files"][
        "contract_snapshot"]
    frozen = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    encoded = json.dumps(frozen, ensure_ascii=False)
    assert "operator_note" not in encoded
    assert "auth_blob" not in encoded
    assert "reaction-notes" not in encoded
    assert "not-for-derived-artifacts" not in encoded
