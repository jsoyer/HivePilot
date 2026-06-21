"""Tests for the live agent-turn streaming helper (notification_service).

stream_agent_turn pushes an outbound Telegram message per agent turn during a
run. It must format the turn conversationally, honour the telegram_stream_live
toggle, and never raise — Telegram being unconfigured is a silent no-op.
"""

from __future__ import annotations

import pytest

from hivepilot.services import notification_service as ns


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        ns, "_send_telegram", lambda msg, chat_id=None, message_thread_id=None, parse_mode=None: sent.append(msg)
    )
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    return sent


def test_stream_routes_to_dedicated_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        ns,
        "_send_telegram",
        lambda msg, chat_id=None, message_thread_id=None, parse_mode=None: seen.update(chat_id=chat_id),
    )
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", -100123, raising=False)
    ns.stream_agent_turn(actor="Blaise (CTO)", summary="x")
    assert seen["chat_id"] == -100123  # live stream → dedicated channel


def test_streams_actor_target_and_summary(captured: list[str]) -> None:
    ns.stream_agent_turn(actor="Aliénor", stage="CEO Intake", target="Colbert", summary="ok")
    assert len(captured) == 1
    msg = captured[0]
    assert "Aliénor" in msg
    assert "CEO Intake" in msg
    assert "Colbert" in msg
    assert "ok" in msg


def test_disabled_toggle_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(ns, "_send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(ns.settings, "telegram_stream_live", False, raising=False)
    ns.stream_agent_turn(actor="Aliénor", summary="should not send")
    assert sent == []


def test_unconfigured_telegram_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_msg: str, chat_id=None, message_thread_id=None, parse_mode=None) -> None:
        raise ns._NotConfigured("no token")

    monkeypatch.setattr(ns, "_send_telegram", _raise)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    ns.stream_agent_turn(actor="Blaise", summary="x")  # must not raise


def test_long_summary_truncated(captured: list[str]) -> None:
    ns.stream_agent_turn(actor="Gustave", summary="y" * 3000)
    assert "…" in captured[0]
    assert len(captured[0]) < 1700  # ~1500 cap + header headroom


def test_medium_summary_not_truncated(captured: list[str]) -> None:
    # ~1000 chars now fits (cap raised to 1500) so the user sees more detail
    ns.stream_agent_turn(actor="Gustave", summary="z" * 1000)
    assert "…" not in captured[0]


def test_collapses_whitespace_and_newlines(captured: list[str]) -> None:
    ns.stream_agent_turn(actor="Voltaire", summary="line1\n\n   line2")
    assert "line1 line2" in captured[0]


def test_minimal_call_actor_only(captured: list[str]) -> None:
    ns.stream_agent_turn(actor="Diderot")
    assert "Diderot" in captured[0]


def test_emit_event_posts_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ns.settings, "event_webhook_url", "https://n8n/hook", raising=False)
    monkeypatch.setattr(ns.settings, "event_webhook_token", "tok", raising=False)
    sent: dict = {}
    monkeypatch.setattr(
        ns.requests,
        "post",
        lambda url, json, headers, timeout: sent.update(url=url, json=json, headers=headers),
    )
    ns.emit_event("checkpoint", run_id=42, pipeline="company-v2")
    assert sent["url"] == "https://n8n/hook"
    assert sent["json"] == {"event": "checkpoint", "run_id": 42, "pipeline": "company-v2"}
    assert sent["headers"]["Authorization"] == "Bearer tok"


def test_emit_event_noop_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ns.settings, "event_webhook_url", None, raising=False)
    called: list[int] = []
    monkeypatch.setattr(ns.requests, "post", lambda *a, **k: called.append(1))
    ns.emit_event("complete", run_id=1)
    assert called == []


# ---------------------------------------------------------------------------
# Rich HTML card tests
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_rich(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture _send_telegram calls including parse_mode kwarg."""
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "parse_mode": parse_mode})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)
    return calls


def test_rich_card_html_sent_for_structured_summary(
    captured_rich: list[dict],
) -> None:
    """When stream_rich=True and summary is structured, send HTML card."""
    structured = "## status\nPASS\n## summary\n- task done\n- tests green\n"
    ns.stream_agent_turn(actor="Blaise (CTO)", target="Hugo (CISO)", summary=structured)
    assert len(captured_rich) == 1
    call = captured_rich[0]
    assert call["parse_mode"] == "HTML"
    assert "<b>" in call["msg"]  # HTML card, not plain text


def test_rich_card_contains_bullets_not_raw_dump(
    captured_rich: list[dict],
) -> None:
    """The rendered card has bullets from parsed summary, NOT the raw agent dump."""
    structured = "## status\nPASS\n## summary\n- bullet one\n- bullet two\n"
    ns.stream_agent_turn(actor="Blaise (CTO)", summary=structured)
    assert len(captured_rich) == 1
    msg = captured_rich[0]["msg"]
    assert "• bullet one" in msg
    assert "• bullet two" in msg
    # The raw markdown header syntax should not appear verbatim
    assert "## status" not in msg


def test_rich_falls_back_when_stream_rich_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When telegram_stream_rich=False, send plain text without parse_mode."""
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "parse_mode": parse_mode})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)

    structured = "## status\nPASS\n## summary\n- done\n"
    ns.stream_agent_turn(actor="Blaise (CTO)", summary=structured)
    assert len(calls) == 1
    assert calls[0]["parse_mode"] is None
    # Plain text: no HTML tags
    assert "<b>" not in calls[0]["msg"]


def test_rich_falls_back_for_unstructured_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When summary has no structured content, fall back to plain text."""
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "parse_mode": parse_mode})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)

    ns.stream_agent_turn(actor="Blaise (CTO)", summary="Just a plain sentence with no structure.")
    assert len(calls) == 1
    # No parse_mode for unstructured text
    assert calls[0]["parse_mode"] is None
