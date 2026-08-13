from __future__ import annotations

import json
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest

from vcstudio.project.assistant_chat import (
    AssistantChat,
    AttachmentRejected,
    HELP_TEXT,
    MAX_FILE_BYTES,
)


def _config():
    return {
        "base_url": "https://llm.invalid/v1",
        "model": "offline-test-model",
    }


def _reply_transport(calls: list[dict] | None = None):
    def transport(url, body, headers, timeout, cancel_event):
        if calls is not None:
            calls.append(
                {
                    "url": url,
                    "body": json.loads(body),
                    "headers": dict(headers),
                    "timeout": timeout,
                    "cancel_event": cancel_event,
                }
            )
        return 200, {
            "choices": [{"message": {"content": "offline reply"}}],
        }

    return transport


def _chat(tmp_path: Path, **kwargs) -> AssistantChat:
    return AssistantChat(
        tmp_path / "assistant",
        transport=kwargs.pop("transport", _reply_transport()),
        config_loader=kwargs.pop("config_loader", _config),
        key_loader=kwargs.pop("key_loader", lambda: "memory-only-key"),
        **kwargs,
    )


def test_sessions_messages_and_attachments_persist_across_instances(tmp_path):
    calls = []
    backend = _chat(tmp_path, transport=_reply_transport(calls))
    session = backend.create_session("Li-S catalyst")
    source = tmp_path / "INCAR"
    source.write_text("ENCUT = 520\nISPIN = 2\n", encoding="utf-8")
    attachment = backend.attach(session["id"], source)

    result = backend.send(
        session["id"],
        "请检查参数",
        attachment_ids=[attachment["id"]],
    )
    assert result["status"] == "ok"
    assert attachment["kind"] == "vasp"
    assert attachment["sha256"]
    assert "ENCUT = 520" in calls[0]["body"]["messages"][-1]["content"]

    reopened = _chat(tmp_path)
    sessions = reopened.list_sessions()
    assert sessions[0]["id"] == session["id"]
    assert sessions[0]["message_count"] == 2
    assert sessions[0]["attachment_count"] == 1
    history = reopened.history(session["id"])
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[0]["attachments"][0]["id"] == attachment["id"]
    assert history[1]["content"] == "offline reply"


def test_same_named_attachments_use_collision_resistant_sanitized_names(tmp_path):
    backend = _chat(tmp_path)
    session = backend.create_session()
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "weird name (final).txt"
    second = second_dir / "weird name (final).txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    item_a = backend.attach(session["id"], first)
    item_b = backend.attach(session["id"], second)

    assert item_a["stored_name"] != item_b["stored_name"]
    assert " " not in item_a["stored_name"]
    assert "(" not in item_a["stored_name"]
    workspace_files = list(
        (tmp_path / "assistant" / "sessions" / session["id"] / "attachments").iterdir()
    )
    assert {path.name for path in workspace_files} == {
        item_a["stored_name"],
        item_b["stored_name"],
    }


def test_symlink_and_oversized_attachments_are_rejected_without_residue(tmp_path):
    backend = _chat(tmp_path)
    session = backend.create_session()
    target = tmp_path / "real.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(AttachmentRejected):
        backend.attach(session["id"], link)

    oversized = tmp_path / "oversized.dat"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(AttachmentRejected):
        backend.attach(session["id"], oversized)

    assert backend.list_attachments(session["id"]) == []
    attachment_dir = (
        tmp_path / "assistant" / "sessions" / session["id"] / "attachments"
    )
    assert list(attachment_dir.iterdir()) == []


def test_zip_inventory_rejects_traversal_and_never_extracts(tmp_path):
    backend = _chat(tmp_path)
    session = backend.create_session()
    malicious = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../escaped.txt", "must not escape")
        archive.writestr("safe/INCAR", "ENCUT = 520")

    with pytest.raises(AttachmentRejected, match="traversal"):
        backend.attach(session["id"], malicious)

    assert not (tmp_path / "escaped.txt").exists()
    assert backend.list_attachments(session["id"]) == []


def test_zip_inventory_rejects_extreme_compression_ratio(tmp_path):
    backend = _chat(tmp_path)
    session = backend.create_session()
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge-zero-file.dat", b"\x00" * (1024 * 1024))

    with pytest.raises(AttachmentRejected, match="compression bomb"):
        backend.attach(session["id"], bomb)
    assert backend.list_attachments(session["id"]) == []


def test_unselected_attachment_and_local_paths_are_not_sent_to_model(tmp_path):
    calls = []
    key = "super-secret-api-key"
    backend = _chat(
        tmp_path,
        transport=_reply_transport(calls),
        key_loader=lambda: key,
    )
    session = backend.create_session()
    secret = tmp_path / "private-results.txt"
    secret.write_text("PRIVATE_RESULT_SHOULD_STAY_LOCAL", encoding="utf-8")
    attachment = backend.attach(session["id"], secret)

    backend.send(session["id"], "hello without attachment")
    wire = json.dumps(calls[0]["body"], ensure_ascii=False)
    assert "PRIVATE_RESULT_SHOULD_STAY_LOCAL" not in wire
    assert str(tmp_path) not in wire
    assert attachment["stored_name"] not in wire

    database_bytes = (tmp_path / "assistant" / "assistant_chat.sqlite3").read_bytes()
    assert key.encode() not in database_bytes
    for path in (tmp_path / "assistant").rglob("*"):
        if path.is_file() and path.name != attachment["stored_name"]:
            assert key.encode() not in path.read_bytes()


def test_outbound_preview_is_exact_read_only_and_contains_no_locator_or_key(tmp_path):
    calls = []
    key = "memory-only-preview-key"
    backend = _chat(
        tmp_path,
        transport=_reply_transport(calls),
        key_loader=lambda: key,
    )
    session = backend.create_session()
    source = tmp_path / "private" / "INCAR"
    source.parent.mkdir()
    source.write_text("ENCUT = 520\n", encoding="utf-8")
    attachment = backend.attach(session["id"], source)

    preview = backend.outbound_preview(
        session["id"], "check this input", attachment_ids=[attachment["id"]]
    )

    assert preview["schema"] == "vcstudio.ai-outbound-preview/v1"
    assert preview["destination"] == "https://llm.invalid"
    assert preview["model"] == "offline-test-model"
    assert preview["selected_attachment_count"] == 1
    assert "ENCUT = 520" in preview["messages"][-1]["content"]
    wire = json.dumps(preview, ensure_ascii=False)
    assert str(tmp_path) not in wire
    assert key not in wire
    assert backend.history(session["id"]) == []
    assert calls == []

    backend.send(
        session["id"], "check this input", attachment_ids=[attachment["id"]]
    )
    assert calls[0]["body"]["messages"] == preview["messages"]


def test_unknown_binary_sends_metadata_only_when_explicitly_selected(tmp_path):
    calls = []
    backend = _chat(tmp_path, transport=_reply_transport(calls))
    session = backend.create_session()
    binary = tmp_path / "sample.bin"
    binary.write_bytes(b"\x00TOP_SECRET_BINARY_PAYLOAD\xff")
    attachment = backend.attach(session["id"], binary)
    assert attachment["kind"] == "binary"
    assert attachment["preview"] is None

    backend.send(session["id"], "inspect metadata", attachment_ids=[attachment["id"]])
    wire = json.dumps(calls[0]["body"], ensure_ascii=False)
    assert "TOP_SECRET_BINARY_PAYLOAD" not in wire
    assert attachment["sha256"] in wire
    assert "metadata only" in wire


def test_stop_event_suppresses_late_transport_result(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    result_holder = {}
    observed_cancel_event = {}

    def blocking_transport(url, body, headers, timeout, cancel_event):
        observed_cancel_event["event"] = cancel_event
        entered.set()
        assert release.wait(5)
        return 200, {
            "choices": [{"message": {"content": "LATE_REPLY_MUST_NOT_PERSIST"}}],
        }

    backend = _chat(tmp_path, transport=blocking_transport)
    session = backend.create_session()

    thread = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result", backend.send(session["id"], "long request")
        )
    )
    thread.start()
    assert entered.wait(5)
    stopped = backend.stop(session["id"])
    assert stopped["stopped"] is True
    assert observed_cancel_event["event"].is_set()
    release.set()
    thread.join(5)
    assert not thread.is_alive()

    assert result_holder["result"]["status"] == "cancelled"
    history = backend.history(session["id"])
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert history[0]["status"] == "cancelled"
    assert all("LATE_REPLY_MUST_NOT_PERSIST" not in item["content"] for item in history)


def test_help_is_deterministic_offline_and_does_not_load_credentials(tmp_path):
    called = {"transport": 0, "config": 0, "key": 0}

    def forbidden_transport(*args):
        called["transport"] += 1
        raise AssertionError("help must not call transport")

    def config_loader():
        called["config"] += 1
        raise AssertionError("help must not load config")

    def key_loader():
        called["key"] += 1
        raise AssertionError("help must not load key")

    backend = AssistantChat(
        tmp_path / "assistant",
        transport=forbidden_transport,
        config_loader=config_loader,
        key_loader=key_loader,
    )
    session = backend.create_session()
    result = backend.send(session["id"], "  /HeLp  ")

    assert result["status"] == "local"
    assert result["content"] == HELP_TEXT
    assert called == {"transport": 0, "config": 0, "key": 0}
    assert [item["source"] for item in backend.history(session["id"])] == [
        "local",
        "local",
    ]


def test_existing_four_argument_transport_is_supported_without_retry(tmp_path):
    calls = []

    def four_argument_transport(url, body, headers, timeout):
        calls.append(json.loads(body))
        return {"choices": [{"message": {"content": "compatible"}}]}

    backend = _chat(tmp_path, transport=four_argument_transport)
    session = backend.create_session()
    result = backend.send(session["id"], "hello")

    assert result["content"] == "compatible"
    assert len(calls) == 1


def test_transport_error_body_that_echoes_key_is_not_persisted(tmp_path):
    key = "memory-only-never-persist"

    def failing_transport(url, body, headers, timeout, cancel_event):
        return 401, json.dumps({"echo": headers["Authorization"]}).encode()

    backend = _chat(
        tmp_path,
        transport=failing_transport,
        key_loader=lambda: key,
    )
    session = backend.create_session()
    with pytest.raises(Exception, match="HTTP 401"):
        backend.send(session["id"], "hello")

    assert backend.history(session["id"])[0]["status"] == "error"
    database = tmp_path / "assistant" / "assistant_chat.sqlite3"
    assert key.encode() not in database.read_bytes()
    with sqlite3.connect(database) as conn:
        values = "\n".join(
            row[0] for row in conn.execute("SELECT content FROM messages")
        )
    assert key not in values


def test_module_has_no_shell_execution_path():
    module_text = Path(
        __file__
    ).parents[1].joinpath("vcstudio/project/assistant_chat.py").read_text(encoding="utf-8")
    assert "subprocess" not in module_text
    assert "os.system" not in module_text
    assert "shell=True" not in module_text
