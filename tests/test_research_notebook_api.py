"""Web-bridge regressions for the Research Notebook transaction boundary."""
from __future__ import annotations

import hashlib
import inspect
import json

from vcstudio.gui_web.api import Api
from vcstudio.project.research_notebook import ResearchNotebook


PROJECT_ID = "project-" + "a" * 32


def _notebook_api(tmp_path, *, dialog_fn=None):
    project_root = tmp_path / "project"
    project_root.mkdir()
    identity = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()
    record = {
        "project_id": PROJECT_ID,
        "path": str(project_root),
        "identity_fingerprint": identity,
    }
    api = Api(dialog_fn=dialog_fn)

    def resolve(candidate):
        if candidate != PROJECT_ID:
            raise ValueError("registered opaque project identity is invalid")
        return dict(record)

    api._report_workbench_project_record = resolve
    api._call_with_project_bindings = lambda _records, operation: operation()
    api._research_notebook_ledger = lambda _record: ResearchNotebook(
        project_root,
        PROJECT_ID,
        project_identity_digest=identity,
        anchor_root=tmp_path / "anchors",
    )
    return api, identity


def _guard(view):
    return {
        "revision": view["revision"],
        "head_digest": view["head_digest"],
        "project_identity_digest": view["project_identity_digest"],
    }


def _request(body="Measured observation", *, attachment_token=None):
    return {
        "record_type": "note",
        "category": "observation",
        "body": body,
        "actor": {"id": "alice", "display_name": "Alice", "role": "researcher"},
        "links": [],
        "supersedes": None,
        "attachment_selection_token": attachment_token,
    }


def test_append_requires_and_returns_stable_core_idempotency_receipt(tmp_path):
    api, _identity = _notebook_api(tmp_path)
    initial = api.research_notebook_bootstrap(PROJECT_ID)
    expected = _guard(initial)
    key = "notebook-operation.api-append-1"

    first = api.research_notebook_append(
        PROJECT_ID, _request(), expected, key)
    replay = api.research_notebook_append(
        PROJECT_ID, _request(), expected, key)

    assert first["ok"] is True and first["revision"] == 1
    assert first["operation_receipt"] == {
        "idempotency_key": key,
        "request_digest": first["operation_receipt"]["request_digest"],
        "revision": 1,
        "record_id": first["created_record_id"],
        "record_digest": first["operation_receipt"]["record_digest"],
        "replayed": False,
    }
    assert replay["ok"] is True and replay["revision"] == 1
    assert replay["created_record_id"] == first["created_record_id"]
    assert replay["operation_receipt"] == {
        **first["operation_receipt"], "replayed": True,
    }
    assert len(replay["records"]) == 1

    reused = api.research_notebook_append(
        PROJECT_ID, _request("Different semantic body"), expected, key)
    missing = api.research_notebook_append(
        PROJECT_ID, _request(), expected)
    assert reused["ok"] is False and reused["error_code"] == "notebook_error"
    assert "different request" in reused["error"]
    assert missing["ok"] is False and missing["operation_receipt"] is None
    assert api.research_notebook_bootstrap(PROJECT_ID)["revision"] == 1


def test_conflict_returns_authoritative_cas_then_same_key_can_retry(tmp_path):
    api, _identity = _notebook_api(tmp_path)
    initial = api.research_notebook_bootstrap(PROJECT_ID)
    api.research_notebook_append(
        PROJECT_ID, _request("first"), _guard(initial),
        "notebook-operation.api-first",
    )
    stale_key = "notebook-operation.api-after-conflict"

    conflict = api.research_notebook_append(
        PROJECT_ID, _request("second"), _guard(initial), stale_key)

    assert conflict["ok"] is False
    assert conflict["error_code"] == "revision_conflict"
    assert conflict["revision"] == 1
    assert conflict["head_digest"]
    assert conflict["operation_receipt"] is None

    current = api.research_notebook_bootstrap(PROJECT_ID)
    retried = api.research_notebook_append(
        PROJECT_ID, _request("second"), _guard(current), stale_key)
    assert retried["ok"] is True and retried["revision"] == 2
    assert retried["operation_receipt"]["idempotency_key"] == stale_key


def test_attachment_selection_is_path_free_and_replay_bound_to_operation(tmp_path):
    attachment = tmp_path / "private-evidence.txt"
    attachment.write_text("bounded evidence bytes", encoding="utf-8")
    api, _identity = _notebook_api(
        tmp_path, dialog_fn=lambda kind: [str(attachment)] if kind == "files" else None)
    initial = api.research_notebook_bootstrap(PROJECT_ID)
    expected = _guard(initial)

    selected = api.research_notebook_pick_attachments(PROJECT_ID, expected)
    encoded_selection = json.dumps(selected, ensure_ascii=False)
    assert selected["ok"] is True and selected["cancelled"] is False
    assert selected["files"][0]["name"] == attachment.name
    assert str(tmp_path) not in encoded_selection
    token = selected["selection_token"]
    key = "notebook-operation.api-attachment"
    request = _request("note with evidence", attachment_token=token)

    first = api.research_notebook_append(PROJECT_ID, request, expected, key)
    replay = api.research_notebook_append(PROJECT_ID, request, expected, key)
    wrong_owner = api.research_notebook_append(
        PROJECT_ID, request, _guard(replay), "notebook-operation.other-owner")

    assert first["ok"] is True
    assert replay["ok"] is True
    assert replay["operation_receipt"]["replayed"] is True
    assert replay["records"][0]["attachments"][0]["name"] == attachment.name
    assert wrong_owner["ok"] is False
    assert api.research_notebook_bootstrap(PROJECT_ID)["revision"] == 1


def test_tombstone_receipt_replays_without_a_second_tombstone(tmp_path):
    api, _identity = _notebook_api(tmp_path)
    initial = api.research_notebook_bootstrap(PROJECT_ID)
    created = api.research_notebook_append(
        PROJECT_ID, _request(), _guard(initial),
        "notebook-operation.api-create-for-delete",
    )
    expected = _guard(created)
    actor = {"id": "alice", "display_name": "Alice", "role": "researcher"}
    key = "notebook-operation.api-tombstone"

    deleted = api.research_notebook_tombstone(
        PROJECT_ID, created["created_record_id"], "Explicit local removal.",
        actor, expected, key)
    replay = api.research_notebook_tombstone(
        PROJECT_ID, created["created_record_id"], "Explicit local removal.",
        actor, expected, key)

    assert deleted["ok"] is True and deleted["revision"] == 2
    assert replay["ok"] is True and replay["revision"] == 2
    assert replay["operation_receipt"]["replayed"] is True
    assert len(replay["records"]) == 2


def test_success_and_failure_dtos_recursively_remove_private_authority(tmp_path):
    api, identity = _notebook_api(tmp_path)
    private_path = str(tmp_path / "secret" / "project.yaml")

    class MaliciousLedger:
        def read(self, *, resolver=None):
            del resolver
            return {
                "schema": "vcstudio.research-notebook-public/v1",
                "ok": True,
                "project_id": PROJECT_ID,
                "project_identity_digest": identity,
                "revision": 0,
                "head_digest": None,
                "integrity_status": "current",
                "records": [{
                    "record_id": "rn-" + "b" * 32,
                    "revision": 1,
                    "body": f"See {private_path}; password=bridge-secret",
                    "nested": {
                        "path": private_path,
                        "command": "srun -n 96 vasp_std",
                        "password": "bridge-secret",
                    },
                }],
                "active_records": [],
                "review_todo": [],
                "limitations": [],
                "denominator": {"records": 1, "active": 0, "review_todo": 0},
            }

    api._research_notebook_ledger = lambda _record: MaliciousLedger()
    success = api.research_notebook_bootstrap(PROJECT_ID)
    encoded = json.dumps(success, ensure_ascii=False)
    assert private_path not in encoded
    assert "bridge-secret" not in encoded
    assert "srun -n 96" not in encoded
    assert success["records"][0]["nested"] == {}

    def fail_resolution(_candidate):
        raise OSError(
            f"failed at {private_path}; password=bridge-secret; command=srun -n 96")

    api._report_workbench_project_record = fail_resolution
    failure = api.research_notebook_bootstrap(PROJECT_ID)
    encoded_failure = json.dumps(failure, ensure_ascii=False)
    assert failure["ok"] is False
    assert private_path not in encoded_failure
    assert "bridge-secret" not in encoded_failure
    assert "srun -n 96" not in encoded_failure


def test_merge_preserves_representative_constructor_and_endpoint_surfaces():
    parameters = set(inspect.signature(Api.__init__).parameters)
    assert {
        "report_service", "research_index_service", "research_view_store",
        "reaction_domain_source", "kinetics_projection_provider",
        "catalysis_authoring_service_factory", "kinetics_authoring_service_factory",
        "trajectory_review_service", "references_mod", "method_recipe_service",
        "structure_sources_mod", "external_reference_gateway",
    } <= parameters

    for endpoint in (
        "research_explorer_bootstrap", "trajectory_open",
        "catalysis_authoring_bootstrap", "kinetics_model_spec_preview",
        "external_reference_catalog", "structure_source_capabilities",
        "method_recipe_catalog", "reaction_presets", "report_workbench_bootstrap",
        "report_evidence_graph", "research_notebook_bootstrap",
        "research_notebook_append", "research_notebook_tombstone",
    ):
        assert callable(getattr(Api, endpoint, None)), endpoint


def test_capsule_bridge_is_receipt_bound_and_old_live_signature_fails_closed(tmp_path):
    service = object()
    identity = "b" * 64
    record = {
        "project_id": PROJECT_ID,
        "path": str(tmp_path / "project.yaml"),
        "identity_fingerprint": identity,
    }
    calls = []

    class Receipts:
        def preview(self, *args, **kwargs):
            calls.append(("preview", args, kwargs))
            return {
                "schema": "vcstudio.report-si-capsule-preview/v1",
                "ok": True,
                "receipt_id": "capsule-receipt.opaque",
                "receipt_token": "capsule-confirm.opaque",
            }

        def confirm(self, *args, **kwargs):
            calls.append(("confirm", args, kwargs))
            if args[4] is None:
                raise ValueError("capsule idempotency key is invalid")
            return {
                "schema": "vcstudio.report-si-capsule/v1",
                "ok": True,
                "file": {"name": "report-r1-si-capsule.zip", "sha256": "c" * 64},
            }

    api = Api(report_service=service)
    api._report_insight_record = lambda candidate: (
        dict(record) if candidate == PROJECT_ID
        else (_ for _ in ()).throw(ValueError("opaque project id required")))
    api._call_with_project_bindings = lambda _records, operation: operation()
    api._report_capsule_receipt_registry = lambda: Receipts()

    preview = api.report_capsule_preview(
        PROJECT_ID, "report-r1", "capsule-destination.opaque",
        "capsule-operation.preview-1")
    confirmed = api.report_capsule_export(
        PROJECT_ID, "capsule-receipt.opaque", "capsule-confirm.opaque",
        "capsule-operation.preview-1")
    legacy = api.report_capsule_export(
        PROJECT_ID, "report-r1", "capsule-destination.opaque")

    assert preview["ok"] is True and confirmed["ok"] is True
    preview_call = calls[0]
    assert preview_call[1] == (
        service, record["path"], "report-r1", "capsule-destination.opaque",
        "capsule-operation.preview-1",
    )
    assert preview_call[2] == {
        "project_id": PROJECT_ID, "identity_fingerprint": identity,
    }
    confirm_call = calls[1]
    assert confirm_call[1][1:5] == (
        record["path"], "capsule-receipt.opaque", "capsule-confirm.opaque",
        "capsule-operation.preview-1",
    )
    assert legacy["ok"] is False and legacy["status"] == "unavailable"
    assert legacy["file"] is None


def test_capsule_picker_and_receipts_share_one_server_side_destination_registry(tmp_path):
    destination = tmp_path / "capsules"
    destination.mkdir()
    api = Api(dialog_fn=lambda kind: str(destination) if kind == "dir" else None)

    selected = api.report_capsule_pick_destination()
    receipts = api._report_capsule_receipt_registry()

    assert selected["ok"] is True and selected["destination_token"]
    assert str(destination) not in json.dumps(selected, ensure_ascii=False)
    assert receipts.destinations is api._report_insight_destinations
    assert api._report_capsule_receipt_registry() is receipts
