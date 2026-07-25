"""Tests for the live agent-turn streaming helper (notification_service).

stream_agent_turn pushes an outbound Telegram message per agent turn during a
run. It must format the turn conversationally, honour the telegram_stream_live
toggle, and never raise — Telegram being unconfigured is a silent no-op.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hivepilot.services import config_provenance
from hivepilot.services import notification_service as ns


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        ns,
        "_send_telegram",
        lambda msg, chat_id=None, message_thread_id=None, parse_mode=None: sent.append(msg),
    )
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    return sent


def test_stream_routes_to_dedicated_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        ns,
        "_send_telegram",
        lambda msg, chat_id=None, message_thread_id=None, parse_mode=None: seen.update(
            chat_id=chat_id
        ),
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


def test_long_summary_not_truncated(captured: list[str]) -> None:
    """A long summary (rich disabled) is no longer clipped with a trailing
    "…" — full content is preserved (split across messages if it doesn't
    fit in one)."""
    ns.stream_agent_turn(actor="Gustave", summary="y" * 3000)
    assert "…" not in captured[0]
    # All 3000 "y"s must be present somewhere in what was sent.
    assert "y" * 3000 in "".join(captured)


def test_very_long_summary_splits_into_multiple_messages(captured: list[str]) -> None:
    """A summary far bigger than one Telegram message (rich disabled) is
    split into several ordered plain-text messages, not cut off."""
    huge = "word " * 2000  # ~10KB, no truncation-hostile long single token
    ns.stream_agent_turn(actor="Gustave", summary=huge)
    assert len(captured) > 1
    for chunk in captured:
        assert len(chunk) <= 3950  # split limit + continuation marker
    # Every chunk after the first is plain text (no HTML tags leaked in).
    for chunk in captured:
        assert "<b>" not in chunk
    # Nothing was silently dropped: every chunk's continuation marker
    # accounts for its position.
    assert "(1/" in captured[0]


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


def test_stream_redacts_registered_secret_in_summary(captured: list[str]) -> None:
    """A resolved ${secret:NAME} value echoed into a stage's agent output must
    never reach the Telegram live-stream — same leak class as send_notification."""
    marker = "STREAM-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)
    ns.stream_agent_turn(actor="Aliénor", summary=f"deployed with {marker}")
    assert len(captured) == 1
    assert marker not in captured[0]
    assert config_provenance.REDACTED in captured[0]


def test_emit_event_posts_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ns.settings, "event_webhook_url", "https://n8n/hook", raising=False)
    monkeypatch.setattr(ns.settings, "event_webhook_token", "tok", raising=False)
    sent: dict = {}
    monkeypatch.setattr(
        ns.requests,
        "post",
        lambda url, json, headers, timeout: sent.update(url=url, json=json, headers=headers),
    )
    ns.emit_event("checkpoint", run_id=42, pipeline="default")
    assert sent["url"] == "https://n8n/hook"
    assert sent["json"] == {"event": "checkpoint", "run_id": 42, "pipeline": "default"}
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
    """A SHORT unstructured summary keeps the legacy plain rendering
    unchanged — short status/heartbeat turns that already worked are left
    alone (only long hand-off/stage-output text gets the new formatting)."""
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


def test_rich_long_unstructured_summary_gets_html_formatting_no_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LONG unstructured hand-off (no ## headers, just free-form agent
    prose) — the case that used to get clipped at ~1500 chars — now renders
    with readable HTML and is never cut off."""
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "parse_mode": parse_mode})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)

    body = "Implemented the **auth middleware** and wired it into the router.\n\n" + (
        "Ran the full test suite, all green. " * 100
    )
    ns.stream_agent_turn(actor="Blaise (CTO)", summary=body)

    assert len(calls) >= 1
    assert all(c["parse_mode"] == "HTML" for c in calls)
    joined = "".join(c["msg"] for c in calls)
    assert "<b>auth middleware</b>" in joined
    # No content silently clipped with a "…" (the old truncation marker).
    assert "…" not in joined


def test_message_thread_id_preserved_across_all_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every chunk of a split turn lands in the SAME topic (message_thread_id)."""
    thread_ids: list[int | None] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        thread_ids.append(message_thread_id)

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns, "_ensure_topic_thread", lambda agent_key, title: 777)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_topics", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", -100999, raising=False)

    huge = "word " * 2000
    ns.stream_agent_turn(actor="Gustave (Developer)", summary=huge)

    assert len(thread_ids) > 1  # actually split into multiple chunks
    assert all(tid == 777 for tid in thread_ids)


def test_long_summary_secret_redaction_preserved_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret echoed inside a LONG summary must be redacted in every
    chunk it ends up in, not just the first message."""
    calls: list[str] = []
    monkeypatch.setattr(
        ns,
        "_send_telegram",
        lambda msg, chat_id=None, message_thread_id=None, parse_mode=None: calls.append(msg),
    )
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)

    marker = "STREAM-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)
    padding = "filler text " * 400
    ns.stream_agent_turn(actor="Aliénor", summary=f"{padding}deployed with {marker}{padding}")

    joined = "".join(calls)
    assert marker not in joined
    assert config_provenance.REDACTED in joined


def test_formatted_chunk_send_failure_falls_back_to_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Telegram rejects a formatted (HTML) chunk with an error (simulated
    400), the SAME chunk is retried as plain text so content still reaches
    the operator even when formatting fails."""
    attempts: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        attempts.append({"msg": msg, "parse_mode": parse_mode})
        if parse_mode == "HTML":
            raise RuntimeError("400 Bad Request: can't parse entities")

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)

    structured = "## status\nPASS\n## summary\n- **bold** thing done\n"
    ns.stream_agent_turn(actor="Blaise (CTO)", summary=structured)

    # First attempt was HTML and failed, second was the plain-text retry.
    assert len(attempts) == 2
    assert attempts[0]["parse_mode"] == "HTML"
    assert attempts[1]["parse_mode"] is None
    assert "<b>" not in attempts[1]["msg"]
    assert "bold" in attempts[1]["msg"]
