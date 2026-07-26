"""Tests for notification_service.stream_agent_turn's channel-agnostic
fan-out: Telegram + Slack + Discord each get an agent turn independently
when enabled, best-effort, never dropping content silently and never letting
one channel's failure block another.
"""

from __future__ import annotations

import pytest

from hivepilot.services import config_provenance
from hivepilot.services import notification_service as ns
from hivepilot.streaming.base import STREAM_CHANNEL_MAP, ThreadRef


@pytest.fixture(autouse=True)
def _restore_stream_channel_map():
    snapshot = dict(STREAM_CHANNEL_MAP)
    yield
    STREAM_CHANNEL_MAP.clear()
    STREAM_CHANNEL_MAP.update(snapshot)


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


class _FakeChannel:
    """A minimal StreamChannel double -- records every send() call."""

    def __init__(self, name: str, *, enabled: bool = True, fail: bool = False) -> None:
        self.name = name
        self.max_len = 1000
        self._enabled = enabled
        self._fail = fail
        self.sent: list[str] = []
        self.threads_requested: list[str] = []

    def enabled(self) -> bool:
        return self._enabled

    def ensure_agent_thread(self, agent_key: str, title: str):
        self.threads_requested.append(agent_key)
        return ThreadRef(channel=self.name, container="c", thread_id="t")

    def send(self, thread, text, *, rich: bool = False) -> None:
        if self._fail:
            raise RuntimeError(f"{self.name} boom")
        self.sent.append(text)

    def format(self, markdown: str) -> str:
        return markdown


@pytest.fixture(autouse=True)
def _telegram_disabled(monkeypatch: pytest.MonkeyPatch):
    """Keep the dedicated Telegram fast-path a no-op in these fan-out-focused
    tests unless a test explicitly re-enables it."""
    monkeypatch.setattr(ns.settings, "telegram_stream_live", False, raising=False)


def test_no_channels_enabled_is_silent_noop() -> None:
    STREAM_CHANNEL_MAP.clear()
    fake = _FakeChannel("fake", enabled=False)
    STREAM_CHANNEL_MAP["fake"] = fake

    ns.stream_agent_turn(actor="Blaise (CTO)", summary="hello")  # must not raise

    assert fake.sent == []


def test_fanout_reaches_every_enabled_non_telegram_channel() -> None:
    STREAM_CHANNEL_MAP.clear()
    slack = _FakeChannel("slack", enabled=True)
    discord = _FakeChannel("discord", enabled=True)
    STREAM_CHANNEL_MAP["slack"] = slack
    STREAM_CHANNEL_MAP["discord"] = discord

    ns.stream_agent_turn(actor="Blaise (CTO)", stage="planning", summary="hello world")

    assert len(slack.sent) == 1
    assert len(discord.sent) == 1
    assert "Blaise" in slack.sent[0]
    assert "Blaise" in discord.sent[0]


def test_one_channel_failure_does_not_block_another() -> None:
    STREAM_CHANNEL_MAP.clear()
    broken = _FakeChannel("slack", enabled=True, fail=True)
    healthy = _FakeChannel("discord", enabled=True)
    STREAM_CHANNEL_MAP["slack"] = broken
    STREAM_CHANNEL_MAP["discord"] = healthy

    ns.stream_agent_turn(actor="Gustave", summary="deploy finished")  # must not raise

    assert healthy.sent == [healthy.sent[0]]  # still delivered
    assert len(healthy.sent) == 1


def test_disabled_channel_is_skipped_without_calling_send() -> None:
    STREAM_CHANNEL_MAP.clear()
    disabled = _FakeChannel("slack", enabled=False)
    STREAM_CHANNEL_MAP["slack"] = disabled

    ns.stream_agent_turn(actor="Gustave", summary="x")

    assert disabled.sent == []
    assert disabled.threads_requested == []


def test_secret_redacted_before_reaching_any_channel() -> None:
    STREAM_CHANNEL_MAP.clear()
    slack = _FakeChannel("slack", enabled=True)
    STREAM_CHANNEL_MAP["slack"] = slack

    marker = "FANOUT-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)
    ns.stream_agent_turn(actor="Aliénor", summary=f"deployed with {marker}")

    assert len(slack.sent) == 1
    assert marker not in slack.sent[0]
    assert config_provenance.REDACTED in slack.sent[0]


def test_telegram_and_slack_both_enabled_both_receive_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telegram_calls: list[str] = []
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    monkeypatch.setattr(
        ns,
        "_send_telegram",
        lambda msg, chat_id=None, message_thread_id=None, parse_mode=None: telegram_calls.append(
            msg
        ),
    )

    STREAM_CHANNEL_MAP.clear()
    slack = _FakeChannel("slack", enabled=True)
    STREAM_CHANNEL_MAP["slack"] = slack

    ns.stream_agent_turn(actor="Blaise (CTO)", summary="both channels")

    assert len(telegram_calls) == 1
    assert len(slack.sent) == 1


def test_telegram_channel_name_in_map_is_never_double_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'telegram' entry in STREAM_CHANNEL_MAP (the real adapter, registered
    for introspection) must not cause a SECOND Telegram delivery on top of
    the dedicated byte-identical fast path."""
    import hivepilot.streaming  # noqa: F401 -- ensure real channels are registered

    telegram_calls: list[str] = []
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    monkeypatch.setattr(
        ns,
        "_send_telegram",
        lambda msg, chat_id=None, message_thread_id=None, parse_mode=None: telegram_calls.append(
            msg
        ),
    )

    ns.stream_agent_turn(actor="Blaise (CTO)", summary="only once")

    assert len(telegram_calls) == 1
