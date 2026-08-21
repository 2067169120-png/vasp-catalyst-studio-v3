from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from tests.test_reaction_workbench import _rehash_step, _set_domain_origin, projection
from tests.test_reaction_workbench_api import _DomainSource
from tests.test_report_workbench import (
    _html_request,
    _project_id,
    _publish,
    _workbench_api,
)
from vcstudio.project.report_contracts import (
    ReportSnapshot,
    ReportSpec,
    ValidationResult,
    validate_bindings,
)


def _downgrade_preview_to_legacy_present(api, preview):
    """Freeze an equivalent complete bundle using the former present-only binding."""
    record = api._reports()._previews[preview["preview_id"]]
    build = record.build
    binding = build["reaction_projection_binding"]
    raw_hash = binding["source_projection_sha256"]
    old_input = api._bind_reaction_projection_fingerprint(
        api._report_input_fingerprint(build["project"], build["summary"]),
        raw_hash, legacy_present=True)
    old_scientific = api._bind_reaction_projection_fingerprint(
        api._report_scientific_fingerprint(build["project"], build["summary"]),
        raw_hash, legacy_present=True)

    contracts = copy.deepcopy(build["contracts"])
    spec = ReportSpec.from_mapping(contracts["report_spec"])
    snapshot_data = copy.deepcopy(contracts["report_snapshot"])
    snapshot_data["input_fingerprint"] = old_scientific
    snapshot_data["sources"] = [
        item for item in snapshot_data["sources"]
        if item.get("kind") != "canonical_reaction_domain_projection_binding"
    ]
    snapshot_data["evidence"]["reaction_binding"].pop(
        "projection_binding", None)
    snapshot = ReportSnapshot.from_mapping(snapshot_data, spec=spec)
    validation_data = copy.deepcopy(contracts["validation"])
    validation_data["snapshot_sha256"] = snapshot.semantic_sha256
    validation = ValidationResult.from_mapping(
        validation_data, spec=spec, snapshot=snapshot)
    validate_bindings(spec, snapshot, validation)
    refs = {
        "spec": {"schema": spec.schema, "sha256": spec.semantic_sha256},
        "snapshot": {
            "schema": snapshot.schema,
            "sha256": snapshot.semantic_sha256,
            "input_fingerprint": old_scientific,
        },
        "validation": {
            "schema": validation.schema,
            "sha256": validation.semantic_sha256,
            "status": validation.status,
            "final_allowed": validation.final_allowed,
            "report_model_sha256": validation.report_model_sha256,
        },
    }
    contracts.update(
        input_fingerprint=old_scientific,
        report_snapshot=snapshot.to_dict(),
        validation=validation.to_dict(),
        contract_refs=refs,
    )
    model = build["model"]
    model.pop("reaction_projection_binding", None)
    model.update(
        input_fingerprint=old_scientific,
        report_snapshot=snapshot.to_dict(),
        validation=validation.to_dict(),
        contract_refs=refs,
    )
    build.update(
        input_fingerprint=old_input,
        scientific_fingerprint=old_scientific,
        reaction_projection_sha256=raw_hash,
        contracts=contracts,
        model=model,
    )
    build.pop("reaction_projection_binding", None)
    build.pop("reaction_projection_binding_sha256", None)
    record.token.update(
        snapshot_sha256=snapshot.semantic_sha256,
        validation_sha256=validation.semantic_sha256,
    )
    downgraded = copy.deepcopy(preview)
    downgraded["preview_token"] = copy.deepcopy(record.token)
    downgraded["public_snapshot"] = snapshot.to_dict()
    downgraded["validation"] = validation.to_dict()
    downgraded["contract_refs"] = refs
    return downgraded, raw_hash


def test_reaction_binding_enters_snapshot_validation_model_and_html(tmp_path):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    source = _DomainSource(projection())
    api._reaction_domain_source = source
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
        "vcstudio.frozen-reaction-network/v2")
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

    source.value = None
    stale_status = api.proj_report_status(project_id)
    stale_history = api.report_workbench_history(project_id)
    assert stale_status["artifact_current"] is False
    assert stale_status["artifact_status"] == "stale"
    assert stale_history["revisions"][0]["current"] is False
    assert stale_history["revisions"][0]["artifact_status"] == "stale"


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


def test_reaction_projection_absence_is_explicit_stable_and_replayable(tmp_path):
    api, project_path, state, _manifests = _workbench_api(tmp_path)
    source = _DomainSource(None)
    api._reaction_domain_source = source
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    preview = api.report_workbench_preview(project_id, _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=["executive_summary", "adsorption_table", "methods", "limitations"],
    ))

    assert preview["ok"] is True
    binding = preview["public_snapshot"]["evidence"]["reaction_binding"][
        "projection_binding"]
    assert binding["schema"] == "vcstudio.reaction-projection-binding/v1"
    assert binding["state"] == "absent"
    assert re.fullmatch(r"[0-9a-f]{64}", binding["sha256"])
    assert "source_projection_sha256" not in binding
    binding_source = next(
        source for source in preview["public_snapshot"]["sources"]
        if source["kind"] == "canonical_reaction_domain_projection_binding")
    assert binding_source["state"] == "absent"
    assert binding_source["sha256"] == binding["sha256"]
    record = api._reports()._previews[preview["preview_id"]]
    assert record.build["model"]["reaction_projection_binding"] == binding
    assert api._report_workbench_current_state(
        project_path, preview["report_spec"]
    )["reaction_projection_binding"] == binding

    published = _publish(api, project_path, tmp_path / "absent", preview)
    marker = state["project"]["autopilot_report"]
    status = api.proj_report_status(project_id)
    history = api.report_workbench_history(project_id)

    assert published["ok"] is True
    assert marker["reaction_projection_binding"] == binding
    assert marker["reaction_projection_sha256"] == binding["sha256"]
    assert status["artifact_current"] is True
    assert len(history["revisions"]) == 1
    assert history["revisions"][0]["current"] is True
    assert str(tmp_path) not in json.dumps(
        (preview, published, status, history), ensure_ascii=False)

    history_path = tmp_path / ".vcstudio" / "reports" / "history.json"
    raw_history = json.loads(history_path.read_text(encoding="utf-8"))
    raw_entry = next(iter(raw_history["reports"].values()))["revisions"][0]
    raw_entry["artifact_status"] = "generated_unrecorded"
    history_path.write_text(json.dumps(raw_history), encoding="utf-8")
    journal_path = Path(raw_entry["transaction_journal"])
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.update(state="marker_ready", history_ready_recorded=False)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    recovered = api.report_workbench_history(project_id)

    assert recovered["revisions"][0]["artifact_status"] == "ready"
    assert recovered["revisions"][0]["current"] is True
    recovered_journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert recovered_journal["state"] == "complete"

    unbound_marker = copy.deepcopy(marker)
    unbound_marker.pop("reaction_projection_binding")
    unbound_marker["reaction_projection_sha256"] = ""
    state["project"]["autopilot_report"] = unbound_marker
    unbound_status = api.proj_report_status(project_id)
    assert unbound_status["artifact_current"] is False
    assert "reaction projection" in unbound_status["report_reason"]
    state["project"]["autopilot_report"] = marker

    source.value = projection()
    stale_status = api.proj_report_status(project_id)
    stale_history = api.report_workbench_history(project_id)

    assert stale_status["artifact_current"] is False
    assert stale_status["artifact_status"] == "stale"
    assert stale_history["revisions"][0]["current"] is False
    assert stale_history["revisions"][0]["artifact_status"] == "stale"


@pytest.mark.parametrize("starts_present", [False, True])
def test_reaction_projection_presence_transition_invalidates_preview_cas(
        tmp_path, starts_present):
    api, project_path, state, _manifests = _workbench_api(tmp_path)
    source = _DomainSource(projection() if starts_present else None)
    api._reaction_domain_source = source
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    preview = api.report_workbench_preview(project_id, _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=["executive_summary", "adsorption_table", "methods", "limitations"],
    ))
    frozen_binding = preview["public_snapshot"]["evidence"]["reaction_binding"][
        "projection_binding"]
    source.value = None if starts_present else projection()

    result = _publish(api, project_path, tmp_path / "transition", preview)
    current = api._report_workbench_current_state(
        project_path, preview["report_spec"])

    assert current["reaction_projection_binding"] != frozen_binding
    assert result["ok"] is False
    assert result["artifact_status"] == "stale_preview"
    assert "autopilot_report" not in state["project"]
    assert list((tmp_path / "transition").iterdir()) == []


def test_reaction_projection_appearance_after_render_fails_marker_commit(tmp_path):
    api, project_path, state, _manifests = _workbench_api(tmp_path)
    source = _DomainSource(None)
    api._reaction_domain_source = source
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    preview = api.report_workbench_preview(project_id, _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=["executive_summary", "adsorption_table", "methods", "limitations"],
    ))
    render_build = api._report_workbench_render_build

    def render_then_publish_projection(*args, **kwargs):
        rendered = render_build(*args, **kwargs)
        source.value = projection()
        return rendered

    api._report_workbench_render_build = render_then_publish_projection
    result = _publish(api, project_path, tmp_path / "late-transition", preview)
    history = api.report_workbench_history(project_id)

    assert result["ok"] is False
    assert result["artifact_status"] == "generated_unrecorded"
    assert result["stale_input"] is True
    assert result["marker_recorded"] is False
    assert "autopilot_report" not in state["project"]
    assert len(history["revisions"]) == 1
    assert history["revisions"][0]["artifact_status"] == "stale"
    assert history["revisions"][0]["current"] is False
    assert history["revisions"][0]["recovery_state"] is None
    assert str(tmp_path) not in json.dumps((result, history), ensure_ascii=False)


def test_legacy_present_only_complete_bundle_replays_exact_old_binding(tmp_path):
    api, project_path, state, _manifests = _workbench_api(tmp_path)
    source = _DomainSource(projection())
    api._reaction_domain_source = source
    project_id = _project_id(api, project_path)
    boot = api.report_workbench_bootstrap(project_id, "scientific-review")
    preview = api.report_workbench_preview(project_id, _html_request(
        boot["project_id"], preset_id="scientific-review", requested_kind="final",
        outline=["executive_summary", "adsorption_table", "methods", "limitations"],
    ))
    legacy_preview, raw_hash = _downgrade_preview_to_legacy_present(api, preview)
    current_state = api._report_workbench_current_state

    def legacy_current_state(path, report_spec=None):
        context = api._report_workbench_project_context(path)
        summary = api._adsorption.delta_e_rows(context["project"])
        if report_spec is not None:
            summary = api._report_workbench_scope_summary(
                context, summary, ReportSpec.from_mapping(report_spec))
        eligible, reason = api._final_report_gate(context["project"], summary)
        current_binding = api._current_reaction_projection_binding(
            context["project"], context["project_id"])
        current_raw = current_binding.get("source_projection_sha256") or ""
        return {
            "project_id": context["project_id"],
            "input_fingerprint": api._bind_reaction_projection_fingerprint(
                api._report_input_fingerprint(context["project"], summary),
                current_raw, legacy_present=True),
            "scientific_fingerprint": api._bind_reaction_projection_fingerprint(
                api._report_scientific_fingerprint(context["project"], summary),
                current_raw, legacy_present=True),
            "reaction_projection_sha256": current_raw,
            "eligible_final": bool(eligible),
            "gate_reason": str(reason or ""),
        }

    api._report_workbench_current_state = legacy_current_state
    try:
        published = _publish(
            api, project_path, tmp_path / "legacy-present", legacy_preview)
    finally:
        api._report_workbench_current_state = current_state

    marker = state["project"]["autopilot_report"]
    status = api.proj_report_status(project_id)
    history = api.report_workbench_history(project_id)

    assert published["ok"] is True
    assert marker["reaction_projection_sha256"] == raw_hash
    assert "reaction_projection_binding" not in marker
    assert status["artifact_current"] is True
    assert history["revisions"][0]["artifact_status"] == "ready"
    assert history["revisions"][0]["current"] is True

    history_path = tmp_path / ".vcstudio" / "reports" / "history.json"
    raw_history = json.loads(history_path.read_text(encoding="utf-8"))
    raw_entry = next(iter(raw_history["reports"].values()))["revisions"][0]
    raw_entry["artifact_status"] = "generated_unrecorded"
    history_path.write_text(json.dumps(raw_history), encoding="utf-8")
    journal_path = Path(raw_entry["transaction_journal"])
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.update(state="marker_ready", history_ready_recorded=False)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    recovered = api.report_workbench_history(project_id)

    assert recovered["revisions"][0]["artifact_status"] == "ready"
    assert recovered["revisions"][0]["current"] is True
    assert "reaction_projection_binding" not in state[
        "project"]["autopilot_report"]
    assert state["project"]["autopilot_report"][
        "reaction_projection_sha256"] == raw_hash

    changed = copy.deepcopy(source.value)
    changed["bindings"]["state-p"]["thermochemistry"][
        "electronic_energy_e0_eV"]["value"] -= 0.2
    source.value = changed
    changed_status = api.proj_report_status(project_id)
    changed_history = api.report_workbench_history(project_id)
    assert changed_status["artifact_current"] is False
    assert changed_history["revisions"][0]["current"] is False

    source.value = projection()
    empty_marker = copy.deepcopy(marker)
    empty_marker["reaction_projection_sha256"] = ""
    state["project"]["autopilot_report"] = empty_marker
    empty_status = api.proj_report_status(project_id)
    empty_history = api.report_workbench_history(project_id)
    assert empty_status["artifact_current"] is False
    assert empty_history["revisions"][0]["current"] is False

    missing_marker = copy.deepcopy(marker)
    missing_marker.pop("reaction_projection_sha256")
    state["project"]["autopilot_report"] = missing_marker
    missing_status = api.proj_report_status(project_id)
    missing_history = api.report_workbench_history(project_id)
    assert missing_status["artifact_current"] is False
    assert missing_history["revisions"][0]["current"] is False


def test_verified_ts_evidence_can_raise_only_bounded_kinetic_qualification(tmp_path):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    value = projection()
    for binding in value["bindings"].values():
        binding["scientific_status"] = "verified"
        if isinstance(binding.get("saddle"), dict):
            binding["saddle"]["scientific_status"] = "verified"
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


def test_imported_domain_origin_cannot_be_promoted_to_kinetic_report_claim(tmp_path):
    api, project_path, _state, _manifests = _workbench_api(tmp_path)
    value = projection()
    for binding in value["bindings"].values():
        binding["scientific_status"] = "verified"
        if isinstance(binding.get("saddle"), dict):
            binding["saddle"]["scientific_status"] = "verified"
    _set_domain_origin(value["steps"][0], "imported")
    _rehash_step(value)
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
    assert preview["scientific_qualification"] != "kinetic_evidence_verified"
    reaction = preview["public_snapshot"]["payload"]["reaction_workbench"]
    assert reaction["graph"]["scientific_status"] == "machine_pass"
    assert reaction["graph"]["kinetic_ready"] is False
    assert reaction["frozen_network"]["readiness"] == "blocked"


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
