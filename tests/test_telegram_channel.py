"""Tests for hivepilot.streaming.telegram_channel.TelegramStreamChannel --
a thin adapter over notification_service's existing, fully-tested Telegram
pipeline. These tests verify DELEGATION (the adapter calls the exact same
module-level functions), not a reimplementation -- the pipeline itself keeps
its own dedicated test suite (test_stream_topics.py, test_telegram_chunking.py,
test_telegram_formatting.py, test_notification_service.py) UNCHANGED.
"""

from __future__ import annotations

from unittest.mock import patch

from hivepilot.services import notification_service as ns
from hivepilot.streaming.base import ThreadRef
from hivepilot.streaming.telegram_channel import TelegramStreamChannel


class TestEnabled:
    def test_enabled_true_when_stream_live(self, monkeypatch) -> None:
        monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
        assert TelegramStreamChannel().enabled() is True

    def test_enabled_false_when_stream_live_off(self, monkeypatch) -> None:
        monkeypatch.setattr(ns.settings, "telegram_stream_live", False, raising=False)
        assert TelegramStreamChannel().enabled() is False


class TestEnsureAgentThread:
    def test_returns_none_when_topics_disabled(self, monkeypatch) -> None:
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", False, raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", 999, raising=False)
        assert TelegramStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)") is None

    def test_delegates_to_ensure_topic_thread(self, monkeypatch) -> None:
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", True, raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", 999, raising=False)
        with patch(
            "hivepilot.services.notification_service._ensure_topic_thread",
            return_value=42,
        ) as mock_ensure:
            ref = TelegramStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)")
        mock_ensure.assert_called_once_with("cto", "Blaise (CTO)")
        assert ref == ThreadRef(channel="telegram", container=999, thread_id=42)

    def test_returns_none_when_ensure_topic_thread_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", True, raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", 999, raising=False)
        with patch(
            "hivepilot.services.notification_service._ensure_topic_thread",
            return_value=None,
        ):
            assert TelegramStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)") is None


class TestSend:
    def test_delegates_to_send_chunks_with_thread(self, monkeypatch) -> None:
        captured: dict = {}

        def _fake_send_chunks(text, *, chat_id, message_thread_id, parse_mode, html_aware, **_):
            captured.update(
                text=text,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                parse_mode=parse_mode,
                html_aware=html_aware,
            )

        monkeypatch.setattr(ns, "_send_chunks", _fake_send_chunks)
        thread = ThreadRef(channel="telegram", container=-100999, thread_id=7)
        TelegramStreamChannel().send(thread, "<b>hi</b>", rich=True)

        assert captured["chat_id"] == -100999
        assert captured["message_thread_id"] == 7
        assert captured["parse_mode"] == "HTML"
        assert captured["html_aware"] is True

    def test_send_without_thread_falls_back_to_stream_chat_id(self, monkeypatch) -> None:
        captured: dict = {}
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", -100123, raising=False)

        def _fake_send_chunks(text, *, chat_id, message_thread_id, parse_mode, html_aware, **_):
            captured.update(chat_id=chat_id, message_thread_id=message_thread_id)

        monkeypatch.setattr(ns, "_send_chunks", _fake_send_chunks)
        TelegramStreamChannel().send(None, "hi", rich=False)

        assert captured["chat_id"] == -100123
        assert captured["message_thread_id"] is None


class TestFormat:
    def test_format_delegates_to_format_for_telegram_html(self) -> None:
        result = TelegramStreamChannel().format("**bold**")
        assert result == "<b>bold</b>"
        assert result == ns._format_for_telegram_html("**bold**")
