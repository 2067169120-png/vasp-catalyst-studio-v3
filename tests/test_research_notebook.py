"""Research Notebook / Human Review Ledger contract regressions."""
from __future__ import annotations

import json
import multiprocessing

import pytest

from vcstudio.project.research_notebook import (
    AttachmentSelections,
    NotebookError,
    NotebookRevisionConflict,
    ResearchNotebook,
    bind_links,
)


PROJECT_ID = "project-" + "a" * 32


def _human_actor() -> dict:
    return {"id": "alice", "display_name": "Alice", "role": "PI reviewer"}


def _bound_project_link(digest: str = "1" * 64) -> list[dict]:
    return [{
        "kind": "project", "id": PROJECT_ID, "report_revision_id": None,
        "bound_digest": digest,
    }]


def _append_process(root: str, output) -> None:
    notebook = ResearchNotebook(root, PROJECT_ID)
    try:
        row = notebook.append(
            record_type="note", category="observation", body="parallel",
            actor=_human_actor(), expected_revision=0,
        )
    except NotebookRevisionConflict as exc:
        output.put(("conflict", exc.current_revision))
    else:
        output.put(("ok", row["record_id"]))


def test_append_supersede_and_tombstone_never_overwrite_history(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    first = notebook.append(
        record_type="note", category="hypothesis", body="Initial hypothesis",
        actor=_human_actor(), expected_revision=0,
    )
    second = notebook.append(
        record_type="note", category="hypothesis", body="Revised hypothesis",
        actor=_human_actor(), expected_revision=1, supersedes=first["record_id"],
    )
    deleted = notebook.tombstone(
        record_id=second["record_id"], reason="Withdrawn after re-analysis",
        actor=_human_actor(), expected_revision=2,
    )

    view = notebook.read()
    assert view["revision"] == 3
    assert [row["revision"] for row in view["records"]] == [1, 2, 3]
    assert view["records"][0]["active"] is False
    assert view["records"][1]["active"] is False
    assert deleted["record_type"] == "tombstone"
    assert view["active_records"] == []
    lines = (tmp_path / ".vcstudio" / "research-notebook" / "ledger.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["body"] == "Initial hypothesis"


def test_revision_cas_is_fail_closed(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    notebook.append(
        record_type="decision", category="decision", body="Use PBE baseline",
        actor=_human_actor(), expected_revision=0,
    )

    with pytest.raises(NotebookRevisionConflict) as caught:
        notebook.append(
            record_type="note", category="next_step", body="Run convergence",
            actor=_human_actor(), expected_revision=0,
        )

    assert caught.value.current_revision == 1
    assert notebook.read()["revision"] == 1


def test_cross_process_lock_allows_only_one_first_writer(tmp_path):
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    workers = [
        context.Process(target=_append_process, args=(str(tmp_path), output))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    outcomes = sorted(output.get(timeout=5)[0] for _ in workers)

    assert outcomes == ["conflict", "ok"]
    assert ResearchNotebook(tmp_path, PROJECT_ID).read()["revision"] == 1


def test_record_digest_and_chain_tamper_are_visible_not_skipped(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    notebook.append(
        record_type="note", category="observation", body="Untampered result",
        actor=_human_actor(), expected_revision=0,
    )
    path = tmp_path / ".vcstudio" / "research-notebook" / "ledger.jsonl"
    raw = path.read_text(encoding="utf-8").replace(
        "Untampered result", "Tampered result")
    path.write_text(raw, encoding="utf-8", newline="")

    view = notebook.read()

    assert view["ok"] is False
    assert view["integrity_status"] == "tampered"
    assert view["integrity_error"] == "notebook_integrity_error"
    assert view["records"] == []


def test_link_binding_is_revalidated_as_current_stale_or_missing(tmp_path):
    evidence = {"status": "current", "digest": "1" * 64,
                "route": {"id": "project-overview", "project_id": PROJECT_ID}}

    def resolver(_link):
        return dict(evidence)

    links = bind_links([{"kind": "project", "id": PROJECT_ID}], resolver)
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    notebook.append(
        record_type="note", category="observation", body="Bound evidence",
        actor=_human_actor(), expected_revision=0, links=links,
    )
    assert notebook.read(resolver=resolver)["records"][0]["links"][0]["status"] \
        == "current"

    evidence["digest"] = "2" * 64
    stale = notebook.read(resolver=resolver)["records"][0]["links"][0]
    assert stale["status"] == "stale"
    assert stale["bound_digest"] == "1" * 64
    assert stale["current_digest"] == "2" * 64

    evidence["status"] = "stale"
    declared_stale = notebook.read(resolver=resolver)["records"][0]["links"][0]
    assert declared_stale["status"] == "stale"
    assert declared_stale["current_digest"] == "2" * 64

    evidence["status"] = "missing"
    missing = notebook.read(resolver=resolver)["records"][0]["links"][0]
    assert missing["status"] == "missing"
    assert missing["current_digest"] is None


def test_source_binding_requires_an_exact_report_revision():
    with pytest.raises(NotebookError, match="require report_revision_id"):
        bind_links(
            [{"kind": "source", "id": "source-1"}],
            lambda _link: {"status": "current", "digest": "1" * 64},
        )


def test_human_review_is_explicit_self_attribution_not_a_signature_or_gate(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    row = notebook.append(
        record_type="review", category="review",
        body="Methods are acceptable after the listed correction.",
        actor=_human_actor(), expected_revision=0,
        review={
            "decision": "request_changes",
            "requested_changes": ["Clarify the k-point convergence denominator."],
            "signature_attribution": "Alice, entered locally",
            "local_human_attestation": True,
        },
    )

    assert row["actor"]["actor_type"] == "human"
    assert row["actor"]["identity_assurance"] == "self-asserted-local"
    assert row["review"]["cryptographic_signature"] is False
    assert row["review"]["decision"] == "request_changes"
    assert "scientific_qualification" not in row
    assert "human_scientific_reviewed" not in json.dumps(row)
    view = notebook.read()
    assert [item["record_id"] for item in view["review_todo"]] == [row["record_id"]]
    assert any(item["code"] == "report_gate_unchanged" for item in view["limitations"])


@pytest.mark.parametrize("reserved", [
    "actor_type", "reviewer_type", "made_by", "identity_assurance",
    "cryptographic_signature",
])
def test_client_cannot_spoof_server_controlled_actor_assurance(tmp_path, reserved):
    actor = {**_human_actor(), reserved: "human"}
    with pytest.raises(NotebookError, match="server-controlled"):
        ResearchNotebook(tmp_path, PROJECT_ID).append(
            record_type="review", category="review", body="Approve",
            actor=actor, expected_revision=0,
            review={"decision": "approved", "local_human_attestation": True},
        )


def test_ai_proposal_cannot_become_a_human_review(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    with pytest.raises(NotebookError, match="AI proposals"):
        notebook.append(
            record_type="review", category="review", body="AI says approved",
            actor={"id": "assistant", "display_name": "Assistant", "role": "AI"},
            expected_revision=0, ai_proposal=True,
            review={"decision": "approved", "local_human_attestation": True},
        )
    proposal = notebook.append(
        record_type="note", category="interpretation", body="Candidate interpretation",
        actor={"id": "assistant", "display_name": "Assistant", "role": "AI"},
        expected_revision=0, ai_proposal=True,
    )
    assert proposal["actor"]["actor_type"] == "ai"
    assert proposal["actor"]["entry_method"] == "ai-proposal"


def test_public_dto_redacts_paths_rejects_secrets_and_never_exposes_attachment_paths(
    tmp_path,
):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    source = tmp_path / "private.txt"
    source.write_text("attachment content", encoding="utf-8")
    row = notebook.append(
        record_type="note", category="limitation",
        body=(r"Raw input was under C:\Users\alice\private\OUTCAR and "
              "/custom/research/private/OUTCAR; public source "
              "https://example.org/evidence/record remains."),
        actor=_human_actor(), expected_revision=0,
        attachments=[{"name": source.name, "data": source.read_bytes()}],
    )
    serialized = json.dumps(row, ensure_ascii=False)
    assert "C:\\\\Users" not in serialized
    assert row["body"].count("<local-path>") == 2
    assert "https://example.org/evidence/record" in row["body"]
    assert str(tmp_path) not in serialized
    assert set(row["attachments"][0]) == {
        "attachment_id", "name", "sha256", "size", "media_type",
    }

    with pytest.raises(NotebookError, match="credential-like"):
        notebook.append(
            record_type="note", category="observation", body="token=super-secret-value",
            actor=_human_actor(), expected_revision=1,
        )


def test_attachment_selection_is_opaque_project_bound_single_use_and_tamper_checked(
    tmp_path,
):
    selected = tmp_path / "figure.png"
    selected.write_bytes(b"not-really-a-png")
    registry = AttachmentSelections()
    public = registry.register([selected], project_id=PROJECT_ID)
    assert "path" not in json.dumps(public)
    selected.write_bytes(b"changed")
    with pytest.raises(NotebookError, match="changed after selection"):
        registry.consume(public["selection_token"], project_id=PROJECT_ID)
    with pytest.raises(NotebookError, match="invalid or expired"):
        registry.consume(public["selection_token"], project_id=PROJECT_ID)


def test_archive_payload_binds_ledger_head_and_declares_attachment_limit(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    notebook.append(
        record_type="decision", category="decision", body="Retain explicit limitation",
        actor=_human_actor(), expected_revision=0, links=_bound_project_link(),
    )
    payload = notebook.archive_payload(
        report_revision_id="report-r0001",
        resolver=lambda _link: {"status": "current", "digest": "1" * 64},
    )

    assert payload["bound_report_revision_id"] == "report-r0001"
    assert payload["ledger_revision"] == 1
    assert len(payload["ledger_head_digest"]) == 64
    assert payload["records"][0]["links"][0]["status"] == "current"
    assert any(item["code"] == "attachment_bytes_local"
               for item in payload["limitations"])
