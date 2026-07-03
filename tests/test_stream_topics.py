"""Tests for Telegram forum topic routing (per-agent message threads)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from hivepilot.services.notification_service import (
    _load_topics,
    _resolve_agent_key,
    _save_topics,
    _send_telegram,
    stream_agent_turn,
)

# ---------------------------------------------------------------------------
# _resolve_agent_key
# ---------------------------------------------------------------------------


class TestResolveAgentKey:
    def test_cto_by_display_name(self):
        assert _resolve_agent_key("Blaise (CTO)") == "cto"

    def test_developer_by_display_name(self):
        assert _resolve_agent_key("Gustave (Developer)") == "developer"

    def test_ceo_accent(self):
        assert _resolve_agent_key("Aliénor (CEO)") == "ceo"

    def test_chief_of_staff(self):
        key = _resolve_agent_key("Jules (Chief of Staff)")
        assert key == "chief_of_staff"

    def test_unknown_returns_non_empty_slug(self):
        key = _resolve_agent_key("SomethingUnknown (Foo)")
        assert key  # not empty
        assert isinstance(key, str)

    def test_empty_actor_returns_general(self):
        key = _resolve_agent_key("")
        assert key  # not empty


# ---------------------------------------------------------------------------
# _load_topics / _save_topics
# ---------------------------------------------------------------------------


class TestTopicsRegistry:
    def test_round_trip(self, tmp_path: Path, monkeypatch):
        registry_file = tmp_path / "stream_topics.json"
        monkeypatch.setattr(
            "hivepilot.services.notification_service._TOPICS_REGISTRY_PATH",
            registry_file,
        )
        data = {"cto": 42, "developer": 99}
        _save_topics(data)
        loaded = _load_topics()
        assert loaded == data

    def test_load_missing_file_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "hivepilot.services.notification_service._TOPICS_REGISTRY_PATH",
            tmp_path / "nonexistent.json",
        )
        assert _load_topics() == {}

    def test_save_creates_parent_dir(self, tmp_path: Path, monkeypatch):
        nested = tmp_path / "a" / "b" / "topics.json"
        monkeypatch.setattr(
            "hivepilot.services.notification_service._TOPICS_REGISTRY_PATH",
            nested,
        )
        _save_topics({"ceo": 1})
        assert nested.exists()
        assert json.loads(nested.read_text()) == {"ceo": 1}


# ---------------------------------------------------------------------------
# _send_telegram — message_thread_id in payload
# ---------------------------------------------------------------------------


class TestSendTelegramThreadId:
    def _make_settings(self, **kw):
        s = MagicMock()
        s.telegram_bot_token = "tok"
        s.telegram_notification_chat_id = 123
        s.telegram_allowed_chat_ids = []
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_includes_thread_id_when_provided(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["payload"] = json
            r = MagicMock()
            r.raise_for_status = lambda: None
            return r

        with (
            patch("hivepilot.services.notification_service.requests.post", fake_post),
            patch("hivepilot.services.notification_service.settings", self._make_settings()),
        ):
            _send_telegram("hello", chat_id=123, message_thread_id=777)

        assert captured["payload"]["message_thread_id"] == 777

    def test_omits_thread_id_when_none(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["payload"] = json
            r = MagicMock()
            r.raise_for_status = lambda: None
            return r

        with (
            patch("hivepilot.services.notification_service.requests.post", fake_post),
            patch("hivepilot.services.notification_service.settings", self._make_settings()),
        ):
            _send_telegram("hello", chat_id=123, message_thread_id=None)

        assert "message_thread_id" not in captured["payload"]


# ---------------------------------------------------------------------------
# stream_agent_turn — topics enabled vs disabled
# ---------------------------------------------------------------------------


class TestStreamAgentTurnTopics:
    def _make_settings(self, topics_enabled: bool):
        s = MagicMock()
        s.telegram_stream_live = True
        s.telegram_stream_topics = topics_enabled
        s.telegram_stream_chat_id = 999
        s.telegram_bot_token = "tok"
        s.telegram_notification_chat_id = None
        s.telegram_allowed_chat_ids = []
        return s

    def test_topics_enabled_calls_ensure_and_passes_thread_id(self):
        fake_thread_id = 42
        sent_kwargs = {}

        def fake_send(message, chat_id=None, message_thread_id=None, parse_mode=None):
            sent_kwargs["message_thread_id"] = message_thread_id

        with (
            patch("hivepilot.services.notification_service.settings", self._make_settings(True)),
            patch(
                "hivepilot.services.notification_service._ensure_topic_thread",
                return_value=fake_thread_id,
            ) as mock_ensure,
            patch("hivepilot.services.notification_service._send_telegram", fake_send),
        ):
            stream_agent_turn(actor="Blaise (CTO)", stage="planning")

        mock_ensure.assert_called_once()
        assert sent_kwargs["message_thread_id"] == fake_thread_id

    def test_topics_disabled_passes_none_thread_id(self):
        sent_kwargs = {}

        def fake_send(message, chat_id=None, message_thread_id=None, parse_mode=None):
            sent_kwargs["message_thread_id"] = message_thread_id

        with (
            patch("hivepilot.services.notification_service.settings", self._make_settings(False)),
            patch("hivepilot.services.notification_service._send_telegram", fake_send),
        ):
            stream_agent_turn(actor="Blaise (CTO)", stage="planning")

        assert sent_kwargs["message_thread_id"] is None
