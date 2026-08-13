from __future__ import annotations


def test_outbound_preview_requires_external_opt_in(monkeypatch):
    from vcstudio.gui_web.api import Api

    api = Api.__new__(Api)
    api._config = type("Config", (), {"load_config": lambda _self: {"llm": {}}})()
    api._chat = lambda: (_ for _ in ()).throw(AssertionError("chat must not load"))

    result = api.ai_chat_outbound_preview("session", "hello", [])

    assert result["ok"] is False
    assert result["preview"] is None
    assert "未开启" in result["error"]


def test_outbound_preview_projects_backend_disclosure_without_mutation():
    from vcstudio.gui_web.api import Api

    captured = {}

    class Chat:
        def outbound_preview(self, session_id, text, *, attachment_ids):
            captured.update(
                session_id=session_id, text=text, attachment_ids=attachment_ids
            )
            return {
                "schema": "vcstudio.ai-outbound-preview/v1",
                "external": True,
                "destination": "https://llm.invalid",
                "model": "model",
                "messages": [{"role": "user", "content": "hello"}],
                "message_count": 1,
                "character_count": 5,
                "selected_attachment_count": 1,
            }

    api = Api.__new__(Api)
    api._config = type(
        "Config", (),
        {"load_config": lambda _self: {"llm": {"allow_external": True}}},
    )()
    api._chat = lambda: Chat()

    result = api.ai_chat_outbound_preview("session", " hello ", ["attachment-1"])

    assert result["ok"] is True
    assert result["preview"]["destination"] == "https://llm.invalid"
    assert captured == {
        "session_id": "session",
        "text": "hello",
        "attachment_ids": ["attachment-1"],
    }
