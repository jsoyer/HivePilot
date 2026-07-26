"""Tests for hivepilot.streaming.slack_channel.SlackStreamChannel --
thread-per-agent Slack streaming via chat.postMessage + thread_ts.
"""

from __future__ import annotations

import json

import pytest

from hivepilot.services.notification_service import NotConfigured
from hivepilot.streaming.base import ThreadRef
from hivepilot.streaming.slack_channel import SlackStreamChannel


@pytest.fixture(autouse=True)
def _slack_settings(monkeypatch: pytest.MonkeyPatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test", raising=False)
    monkeypatch.setattr(settings, "slack_stream_channel_id", "C123", raising=False)
    return settings


@pytest.fixture
def store_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "stream_threads_slack.json"
    monkeypatch.setattr(
        "hivepilot.streaming.slack_channel.default_thread_store_path",
        lambda name: path,
    )
    return path


class TestEnabled:
    def test_enabled_when_token_and_channel_set(self) -> None:
        assert SlackStreamChannel().enabled() is True

    def test_disabled_without_channel_id(self, monkeypatch, _slack_settings) -> None:
        monkeypatch.setattr(_slack_settings, "slack_stream_channel_id", None, raising=False)
        assert SlackStreamChannel().enabled() is False

    def test_disabled_without_token(self, monkeypatch, _slack_settings) -> None:
        monkeypatch.setattr(_slack_settings, "slack_bot_token", None, raising=False)
        assert SlackStreamChannel().enabled() is False


class TestEnsureAgentThread:
    def test_creates_thread_and_persists(self, monkeypatch, store_path) -> None:
        captured: dict = {}

        class _Resp:
            def json(self):
                return {"ok": True, "ts": "1707330000.000100"}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, headers=headers, payload=json)
            return _Resp()

        monkeypatch.setattr("hivepilot.streaming.slack_channel.requests.post", fake_post)

        ref = SlackStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)")

        assert ref == ThreadRef(channel="slack", container="C123", thread_id="1707330000.000100")
        assert captured["payload"]["channel"] == "C123"
        assert json.loads(store_path.read_text()) == {"cto": "1707330000.000100"}

    def test_reuses_cached_thread_without_creating(self, monkeypatch, store_path) -> None:
        store_path.write_text(json.dumps({"cto": "1707330000.000100"}), encoding="utf-8")

        def fail_post(*a, **k):
            raise AssertionError("must not create a new thread for a cached agent_key")

        monkeypatch.setattr("hivepilot.streaming.slack_channel.requests.post", fail_post)

        ref = SlackStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)")
        assert ref == ThreadRef(channel="slack", container="C123", thread_id="1707330000.000100")

    def test_returns_none_on_api_error(self, monkeypatch, store_path) -> None:
        class _Resp:
            def json(self):
                return {"ok": False, "error": "channel_not_found"}

        monkeypatch.setattr(
            "hivepilot.streaming.slack_channel.requests.post", lambda *a, **k: _Resp()
        )
        assert SlackStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)") is None

    def test_returns_none_on_network_error(self, monkeypatch, store_path) -> None:
        def _raise(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr("hivepilot.streaming.slack_channel.requests.post", _raise)
        assert SlackStreamChannel().ensure_agent_thread("cto", "Blaise (CTO)") is None


class TestSend:
    def test_send_includes_thread_ts(self, monkeypatch) -> None:
        captured: dict = {}

        class _Resp:
            def json(self):
                return {"ok": True}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(payload=json)
            return _Resp()

        monkeypatch.setattr("hivepilot.streaming.slack_channel.requests.post", fake_post)
        thread = ThreadRef(channel="slack", container="C123", thread_id="1707.001")
        SlackStreamChannel().send(thread, "hello")

        assert captured["payload"]["thread_ts"] == "1707.001"
        assert captured["payload"]["channel"] == "C123"
        assert captured["payload"]["text"] == "hello"

    def test_send_without_thread_omits_thread_ts(self, monkeypatch) -> None:
        captured: dict = {}

        class _Resp:
            def json(self):
                return {"ok": True}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(payload=json)
            return _Resp()

        monkeypatch.setattr("hivepilot.streaming.slack_channel.requests.post", fake_post)
        SlackStreamChannel().send(None, "hello")

        assert "thread_ts" not in captured["payload"]

    def test_send_raises_not_configured_without_channel(self, monkeypatch, _slack_settings) -> None:
        monkeypatch.setattr(_slack_settings, "slack_stream_channel_id", None, raising=False)
        with pytest.raises(NotConfigured):
            SlackStreamChannel().send(None, "hello")

    def test_thread_send_failure_degrades_to_threadless(self, monkeypatch) -> None:
        calls = []

        class _OkResp:
            def json(self):
                return {"ok": True}

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            if json.get("thread_ts") is not None:
                raise RuntimeError("thread not found")
            return _OkResp()

        monkeypatch.setattr("hivepilot.streaming.slack_channel.requests.post", fake_post)
        thread = ThreadRef(channel="slack", container="C123", thread_id="dead.ts")

        SlackStreamChannel().send(thread, "hello")  # must not raise

        assert len(calls) == 2
        assert calls[0]["thread_ts"] == "dead.ts"
        assert "thread_ts" not in calls[1]
        assert calls[1]["text"] == "hello"

    def test_total_failure_raises(self, monkeypatch) -> None:
        def _raise(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("hivepilot.streaming.slack_channel.requests.post", _raise)
        with pytest.raises(RuntimeError):
            SlackStreamChannel().send(None, "hello")


class TestFormat:
    def test_bold_converted_to_mrkdwn(self) -> None:
        result = SlackStreamChannel().format("**bold** text")
        assert result == "*bold* text"

    def test_header_converted_to_bold_line(self) -> None:
        result = SlackStreamChannel().format("## Status\nPASS")
        assert "*Status*" in result
        assert "##" not in result


class TestMaxLen:
    def test_max_len_is_reasonable_for_slack(self) -> None:
        assert 0 < SlackStreamChannel.max_len <= 4000
