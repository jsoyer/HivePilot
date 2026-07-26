"""Tests for hivepilot.streaming.discord_channel.DiscordStreamChannel --
thread-per-agent Discord streaming via the channel thread REST API.
"""

from __future__ import annotations

import json

import pytest

from hivepilot.services.notification_service import NotConfigured
from hivepilot.streaming.base import ThreadRef
from hivepilot.streaming.discord_channel import DiscordStreamChannel


@pytest.fixture(autouse=True)
def _discord_settings(monkeypatch: pytest.MonkeyPatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "discord_bot_token", "bot-tok", raising=False)
    monkeypatch.setattr(settings, "discord_stream_channel_id", 555, raising=False)
    return settings


@pytest.fixture
def store_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "stream_threads_discord.json"
    monkeypatch.setattr(
        "hivepilot.streaming.discord_channel.default_thread_store_path",
        lambda name: path,
    )
    return path


class TestEnabled:
    def test_enabled_when_token_and_channel_set(self) -> None:
        assert DiscordStreamChannel().enabled() is True

    def test_disabled_without_channel_id(self, monkeypatch, _discord_settings) -> None:
        monkeypatch.setattr(_discord_settings, "discord_stream_channel_id", None, raising=False)
        assert DiscordStreamChannel().enabled() is False

    def test_disabled_without_token(self, monkeypatch, _discord_settings) -> None:
        monkeypatch.setattr(_discord_settings, "discord_bot_token", None, raising=False)
        assert DiscordStreamChannel().enabled() is False


class TestEnsureAgentThread:
    def test_creates_thread_and_persists(self, monkeypatch, store_path) -> None:
        captured: dict = {}

        class _Resp:
            def json(self):
                return {"id": 999888}

            def raise_for_status(self):
                pass

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, payload=json)
            return _Resp()

        monkeypatch.setattr("hivepilot.streaming.discord_channel.requests.post", fake_post)

        ref = DiscordStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)")

        assert ref == ThreadRef(channel="discord", container=555, thread_id=999888)
        assert "/channels/555/threads" in captured["url"]
        assert json.loads(store_path.read_text()) == {"cto": 999888}

    def test_reuses_cached_thread_without_creating(self, monkeypatch, store_path) -> None:
        store_path.write_text(json.dumps({"cto": 999888}), encoding="utf-8")

        def fail_post(*a, **k):
            raise AssertionError("must not create a new thread for a cached agent_key")

        monkeypatch.setattr("hivepilot.streaming.discord_channel.requests.post", fail_post)

        ref = DiscordStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)")
        assert ref == ThreadRef(channel="discord", container=555, thread_id=999888)

    def test_returns_none_on_error(self, monkeypatch, store_path) -> None:
        def _raise(*a, **k):
            raise RuntimeError("discord down")

        monkeypatch.setattr("hivepilot.streaming.discord_channel.requests.post", _raise)
        assert DiscordStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)") is None


class TestSend:
    def test_send_posts_to_thread_channel(self, monkeypatch) -> None:
        captured: dict = {}

        class _Resp:
            def raise_for_status(self):
                pass

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, payload=json)
            return _Resp()

        monkeypatch.setattr("hivepilot.streaming.discord_channel.requests.post", fake_post)
        thread = ThreadRef(channel="discord", container=555, thread_id=777)
        DiscordStreamChannel().send(thread, "hello")

        assert "/channels/777/messages" in captured["url"]
        assert captured["payload"]["content"] == "hello"
        assert captured["payload"]["allowed_mentions"] == {"parse": []}

    def test_send_without_thread_posts_to_channel(self, monkeypatch) -> None:
        captured: dict = {}

        class _Resp:
            def raise_for_status(self):
                pass

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url)
            return _Resp()

        monkeypatch.setattr("hivepilot.streaming.discord_channel.requests.post", fake_post)
        DiscordStreamChannel().send(None, "hello")

        assert "/channels/555/messages" in captured["url"]

    def test_send_raises_not_configured_without_channel(
        self, monkeypatch, _discord_settings
    ) -> None:
        monkeypatch.setattr(_discord_settings, "discord_stream_channel_id", None, raising=False)
        with pytest.raises(NotConfigured):
            DiscordStreamChannel().send(None, "hello")

    def test_thread_send_failure_degrades_to_parent_channel(self, monkeypatch) -> None:
        calls = []

        class _OkResp:
            def raise_for_status(self):
                pass

        class _FailResp:
            def raise_for_status(self):
                raise RuntimeError("unknown channel (thread deleted)")

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(url)
            if "/channels/777/" in url:
                return _FailResp()
            return _OkResp()

        monkeypatch.setattr("hivepilot.streaming.discord_channel.requests.post", fake_post)
        thread = ThreadRef(channel="discord", container=555, thread_id=777)

        DiscordStreamChannel().send(thread, "hello")  # must not raise

        assert len(calls) == 2
        assert "/channels/777/messages" in calls[0]
        assert "/channels/555/messages" in calls[1]

    def test_total_failure_raises(self, monkeypatch) -> None:
        def _raise(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("hivepilot.streaming.discord_channel.requests.post", _raise)
        with pytest.raises(RuntimeError):
            DiscordStreamChannel().send(None, "hello")


class TestFormat:
    def test_format_is_near_identity_for_standard_markdown(self) -> None:
        md = "**bold** and *italic* and `code`"
        assert DiscordStreamChannel().format(md) == md


class TestMaxLen:
    def test_max_len_matches_discord_cap(self) -> None:
        assert DiscordStreamChannel.max_len == 2000
