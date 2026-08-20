"""Research Notebook / Human Review Ledger contract regressions."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from vcstudio.project.research_notebook import (
    AttachmentSelections,
    NotebookError,
    NotebookRevisionConflict,
    ResearchNotebook,
    bind_links,
    digest_json,
    redact_public_text,
)
from vcstudio.campaign.ledger import is_sensitive
from vcstudio.project.report_insights import redact


PROJECT_ID = "project-" + "a" * 32


def _human_actor() -> dict:
    return {"id": "alice", "display_name": "Alice", "role": "PI reviewer"}


def _bound_project_link(digest: str = "1" * 64) -> list[dict]:
    return [{
        "kind": "project", "id": PROJECT_ID, "report_revision_id": None,
        "bound_digest": digest,
    }]


def _cas(notebook: ResearchNotebook, revision: int = 0,
         head_digest: str | None = None) -> dict:
    return {
        "expected_revision": revision,
        "expected_head_digest": head_digest,
        "expected_project_identity": notebook.project_identity_digest,
    }


def _rewrite_valid_digests(notebook: ResearchNotebook, mutate) -> None:
    journal = notebook.journal_path
    rows = [json.loads(line) for line in journal.read_text(
        encoding="utf-8").splitlines()]
    mutate(rows)
    previous = ""
    for index, row in enumerate(rows, start=1):
        row["revision"] = index
        row["previous_digest"] = previous
        row["record_digest"] = digest_json({
            key: value for key, value in row.items() if key != "record_digest"})
        previous = row["record_digest"]
    journal.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8", newline="")
    anchor = json.loads(notebook.anchor_path.read_text(encoding="utf-8"))
    anchor["sequence"] = len(rows)
    anchor["head_digest"] = previous
    anchor["anchor_digest"] = digest_json({
        key: value for key, value in anchor.items() if key != "anchor_digest"})
    notebook.anchor_path.write_text(
        json.dumps(anchor, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n",
        encoding="utf-8", newline="")


def _append_process(root: str, output) -> None:
    notebook = ResearchNotebook(root, PROJECT_ID)
    try:
        row = notebook.append(
            record_type="note", category="observation", body="parallel",
            actor=_human_actor(), **_cas(notebook),
        )
    except NotebookRevisionConflict as exc:
        output.put(("conflict", exc.current_revision))
    else:
        output.put(("ok", row["record_id"]))


def test_append_supersede_and_tombstone_never_overwrite_history(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    first = notebook.append(
        record_type="note", category="hypothesis", body="Initial hypothesis",
        actor=_human_actor(), **_cas(notebook),
    )
    second = notebook.append(
        record_type="note", category="hypothesis", body="Revised hypothesis",
        actor=_human_actor(), supersedes=first["record_id"],
        **_cas(notebook, 1, first["record_digest"]),
    )
    deleted = notebook.tombstone(
        record_id=second["record_id"], reason="Withdrawn after re-analysis",
        actor=_human_actor(), **_cas(notebook, 2, second["record_digest"]),
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
        actor=_human_actor(), **_cas(notebook),
    )

    with pytest.raises(NotebookRevisionConflict) as caught:
        notebook.append(
            record_type="note", category="next_step", body="Run convergence",
            actor=_human_actor(), **_cas(notebook),
        )

    assert caught.value.current_revision == 1
    assert notebook.read()["revision"] == 1


def test_cas_binds_head_digest_and_project_identity(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    with pytest.raises(NotebookRevisionConflict):
        notebook.append(
            record_type="note", category="observation", body="wrong identity",
            actor=_human_actor(), expected_revision=0,
            expected_head_digest=None, expected_project_identity="f" * 64)
    first = notebook.append(
        record_type="note", category="observation", body="bound CAS",
        actor=_human_actor(), **_cas(notebook))
    with pytest.raises(NotebookRevisionConflict):
        notebook.append(
            record_type="note", category="next_step", body="wrong head",
            actor=_human_actor(), expected_revision=1,
            expected_head_digest="e" * 64,
            expected_project_identity=notebook.project_identity_digest)
    assert notebook.read()["head_digest"] == first["record_digest"]


@pytest.mark.parametrize("damage", ["truncate", "delete"])
def test_external_anchor_rejects_truncated_or_deleted_journal(tmp_path, damage):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    notebook.append(
        record_type="note", category="observation", body="anchored",
        actor=_human_actor(), **_cas(notebook))
    assert notebook.anchor_path.parent != notebook.root
    if damage == "truncate":
        notebook.journal_path.write_bytes(b"")
    else:
        notebook.journal_path.unlink()
    view = notebook.read()
    assert view["ok"] is False
    assert view["integrity_status"] == "tampered"


@pytest.mark.parametrize(
    "attack",
    [
        "duplicate_id", "extra_field", "inactive_supersedes", "wrong_type",
        "actor_schema", "review_schema", "attachment_schema",
        "inactive_tombstone",
    ],
)
def test_reader_replays_exact_full_state_machine_even_after_digest_rewrite(
    tmp_path, attack,
):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    first = notebook.append(
        record_type="note", category="observation", body="first",
        actor=_human_actor(), **_cas(notebook))
    second = notebook.append(
        record_type="note", category="observation", body="second",
        actor=_human_actor(), supersedes=first["record_id"],
        **_cas(notebook, 1, first["record_digest"]))
    notebook.append(
        record_type="note", category="next_step", body="third",
        actor=_human_actor(), **_cas(notebook, 2, second["record_digest"]))

    def mutate(rows):
        third = rows[2]
        if attack == "duplicate_id":
            third["record_id"] = rows[1]["record_id"]
        elif attack == "extra_field":
            third["unexpected"] = True
        elif attack == "inactive_supersedes":
            third["supersedes"] = rows[0]["record_id"]
        elif attack == "wrong_type":
            third["record_type"] = "decision"
            third["category"] = "decision"
            third["supersedes"] = rows[1]["record_id"]
        elif attack == "actor_schema":
            third["actor"]["reviewer_type"] = "human"
        elif attack == "review_schema":
            third["record_type"] = "review"
            third["category"] = "review"
            third["review"] = {
                "decision": "approved", "requested_changes": [],
                "signature_attribution": None, "signature_kind": "none",
                "cryptographic_signature": False,
                "identity_assurance": "self-asserted-local", "extra": True,
            }
        elif attack == "attachment_schema":
            third["attachments"] = [{"unexpected": "blob"}]
        elif attack == "inactive_tombstone":
            third.update({
                "record_type": "tombstone", "category": "tombstone",
                "review": None, "links": [], "attachments": [],
                "supersedes": None, "tombstones": rows[0]["record_id"],
            })

    _rewrite_valid_digests(notebook, mutate)
    assert notebook.read()["ok"] is False


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
        actor=_human_actor(), **_cas(notebook),
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
        actor=_human_actor(), links=links, **_cas(notebook),
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
        actor=_human_actor(), **_cas(notebook),
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
        notebook = ResearchNotebook(tmp_path, PROJECT_ID)
        notebook.append(
            record_type="review", category="review", body="Approve",
            actor=actor, **_cas(notebook),
            review={"decision": "approved", "local_human_attestation": True},
        )


def test_ai_proposal_cannot_become_a_human_review(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    with pytest.raises(NotebookError, match="AI proposals"):
        notebook.append(
            record_type="review", category="review", body="AI says approved",
            actor={"id": "assistant", "display_name": "Assistant", "role": "AI"},
            ai_proposal=True, **_cas(notebook),
            review={"decision": "approved", "local_human_attestation": True},
        )
    proposal = notebook.append(
        record_type="note", category="interpretation", body="Candidate interpretation",
        actor={"id": "assistant", "display_name": "Assistant", "role": "AI"},
        ai_proposal=True, **_cas(notebook),
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
        actor=_human_actor(), **_cas(notebook),
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
            actor=_human_actor(), **_cas(notebook, 1, row["record_digest"]),
        )


def test_attachment_selection_is_opaque_project_bound_single_use_and_tamper_checked(
    tmp_path,
):
    selected = tmp_path / "figure.png"
    selected.write_bytes(b"not-really-a-png")
    registry = AttachmentSelections()
    identity = "f" * 64
    public = registry.register(
        [selected], project_id=PROJECT_ID, project_identity_digest=identity)
    assert "path" not in json.dumps(public)
    selected.write_bytes(b"changed")
    with pytest.raises(NotebookError, match="changed after selection"):
        registry.consume(
            public["selection_token"], project_id=PROJECT_ID,
            project_identity_digest=identity)
    with pytest.raises(NotebookError, match="invalid or expired"):
        registry.consume(
            public["selection_token"], project_id=PROJECT_ID,
            project_identity_digest=identity)


def test_archive_payload_binds_ledger_head_and_declares_attachment_limit(tmp_path):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    notebook.append(
        record_type="decision", category="decision", body="Retain explicit limitation",
        actor=_human_actor(), links=_bound_project_link(), **_cas(notebook),
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


def _symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def test_project_notebook_attachment_and_lock_chains_reject_reparse_links(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    _symlink_or_skip(redirected, project / ".vcstudio", directory=True)
    assert ResearchNotebook(project, PROJECT_ID).read()["ok"] is False

    source = tmp_path / "source.txt"
    source.write_bytes(b"evidence")
    selected_link = tmp_path / "selected-link.txt"
    _symlink_or_skip(source, selected_link)
    with pytest.raises(NotebookError, match="non-reparse regular file"):
        AttachmentSelections().register(
            [selected_link], project_id=PROJECT_ID,
            project_identity_digest="d" * 64)

    clean_project = tmp_path / "clean-project"
    clean_project.mkdir()
    real_anchors = tmp_path / "real-anchors"
    real_anchors.mkdir()
    linked_anchors = tmp_path / "linked-anchors"
    _symlink_or_skip(real_anchors, linked_anchors, directory=True)
    linked_lock_view = ResearchNotebook(
        clean_project, PROJECT_ID, anchor_root=linked_anchors).read()
    assert linked_lock_view["ok"] is False
    assert linked_lock_view["integrity_status"] == "tampered"


@pytest.mark.parametrize("existing_kind", ["wrong_bytes", "directory"])
def test_existing_content_addressed_blob_is_rehashed_and_must_be_regular(
    tmp_path, existing_kind,
):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    content = b"authoritative attachment bytes"
    digest = hashlib.sha256(content).hexdigest()
    notebook.attachments_path.mkdir(parents=True)
    target = notebook.attachments_path / f"{digest}.bin"
    if existing_kind == "wrong_bytes":
        target.write_bytes(b"x" * len(content))
    else:
        target.mkdir()
    with pytest.raises(Exception, match="regular file|digest mismatch"):
        notebook.append(
            record_type="note", category="observation", body="attachment",
            actor=_human_actor(), attachments=[{"name": "data.bin", "data": content}],
            **_cas(notebook))
    assert notebook.read()["revision"] == 0


@pytest.mark.parametrize("damage", ["delete", "same_size_tamper"])
def test_read_and_archive_reverify_attachment_blob_hash(tmp_path, damage):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    content = b"immutable evidence"
    row = notebook.append(
        record_type="note", category="observation", body="attachment",
        actor=_human_actor(), attachments=[{"name": "data.bin", "data": content}],
        **_cas(notebook))
    blob = notebook.attachments_path / f"{row['attachments'][0]['sha256']}.bin"
    if damage == "delete":
        blob.unlink()
    else:
        blob.write_bytes(b"x" * len(content))
    assert notebook.read()["integrity_status"] == "tampered"
    archive = notebook.archive_payload(report_revision_id="report-r0001")
    assert archive["integrity_status"] == "tampered"
    assert archive["records"] == []


@pytest.mark.parametrize("secret", [
    "glpat-abcdefgh123456",
    "hf_abcdefgh1234567890",
    "sk_live_abcdefgh123456",
    "xoxb-12345678-abcdefgh",
    "postgresql://alice:supersecret@db.example/research",
])
def test_shared_credential_classifier_covers_notebook_public_dto_and_si_capsule(
    tmp_path, secret,
):
    notebook = ResearchNotebook(tmp_path, PROJECT_ID)
    with pytest.raises(NotebookError, match="credential-like"):
        notebook.append(
            record_type="note", category="observation", body=f"value {secret}",
            actor=_human_actor(), **_cas(notebook))
    assert is_sensitive(secret)
    assert secret not in redact_public_text(f"value {secret}")
    public = notebook.public_record({
        "record_id": "rn-test", "revision": 1, "record_type": "note",
        "category": "observation", "project_id": PROJECT_ID,
        "created_at_utc": "2026-08-15T00:00:00+00:00",
        "body": f"value {secret}", "actor": {
            "id": "alice", "display_name": "Alice", "role": "PI",
            "actor_type": "human", "entry_method": "local-explicit",
            "identity_assurance": "self-asserted-local",
        }, "review": None, "links": [], "attachments": [],
        "supersedes": None, "tombstones": None, "previous_digest": "",
        "record_digest": "a" * 64,
    }, resolver=None, active=True)
    assert secret not in json.dumps(public)
    assert secret not in json.dumps(redact({"body": f"value {secret}"}))
