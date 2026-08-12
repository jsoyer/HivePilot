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


# ---------------------------------------------------------------------------
# Stale/closed forum-topic self-heal
# ---------------------------------------------------------------------------


class _FakeTelegramError(Exception):
    """Stand-in for TelegramSendError carrying only a `.description`."""

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.status_code = 400
        self.description = description


def _stream_topics_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_topics", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", -100999, raising=False)


def test_is_stale_topic_error_matches_known_variants() -> None:
    assert ns._is_stale_topic_error("Bad Request: message thread not found")
    assert ns._is_stale_topic_error("Bad Request: TOPIC_DELETED")
    assert ns._is_stale_topic_error("the message thread not found")
    assert not ns._is_stale_topic_error("Bad Request: TOPIC_CLOSED")
    assert not ns._is_stale_topic_error(None)


def test_is_closed_topic_error_matches_and_excludes_stale() -> None:
    assert ns._is_closed_topic_error("Bad Request: TOPIC_CLOSED")
    assert not ns._is_closed_topic_error("Bad Request: message thread not found")
    assert not ns._is_closed_topic_error(None)


def test_invalidate_topic_removes_and_persists(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_path = tmp_path / "stream_topics.json"
    monkeypatch.setattr(ns.settings, "stream_topics_registry_path", str(registry_path))
    ns._save_topics({"gustave": 208, "aliénor": 42})

    dead = ns._invalidate_topic("gustave")

    assert dead == 208
    # Persisted: a fresh load no longer contains the dead key.
    assert ns._load_topics() == {"aliénor": 42}
    # A second call finds nothing left to invalidate.
    assert ns._invalidate_topic("gustave") is None


def _sequenced_ensure_topic_thread(*values: int | None):
    """Return a fake `_ensure_topic_thread(agent_key, title)` that yields
    *values* in order on successive calls — the FIRST call resolves the
    initial (soon-to-be-dead) thread id, later calls simulate what the
    self-heal recreate attempt gets back."""
    it = iter(values)

    def _fn(agent_key: str, title: str):
        return next(it)

    return _fn


def test_stale_topic_self_heals_recreates_and_resends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send that 400s with "message thread not found" invalidates the
    registry entry, recreates the topic, and re-sends the SAME content to
    the NEW thread id — the message is delivered, not lost."""
    _stream_topics_settings(monkeypatch)
    # First call (initial resolve) returns the soon-to-be-dead 208; the
    # second call (triggered by the self-heal recreate) returns a fresh 999.
    monkeypatch.setattr(ns, "_ensure_topic_thread", _sequenced_ensure_topic_thread(208, 999))

    invalidated: list[str] = []
    orig_invalidate = ns._invalidate_topic

    def _spy_invalidate(agent_key: str):
        invalidated.append(agent_key)
        return orig_invalidate(agent_key)

    monkeypatch.setattr(ns, "_invalidate_topic", _spy_invalidate)

    calls: list[dict] = []
    call_count = {"n": 0}

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        call_count["n"] += 1
        calls.append({"msg": msg, "message_thread_id": message_thread_id})
        if call_count["n"] == 1:
            raise _FakeTelegramError("Bad Request: message thread not found")

    monkeypatch.setattr(ns, "_send_telegram", _fake)

    ns.stream_agent_turn(actor="Gustave (Developer)", summary="deploy finished")

    assert invalidated == ["developer"]
    assert len(calls) == 2
    assert calls[0]["message_thread_id"] == 208
    assert calls[1]["message_thread_id"] == 999  # NEW thread id, not the dead one
    assert calls[1]["message_thread_id"] != calls[0]["message_thread_id"]
    assert calls[0]["msg"] == calls[1]["msg"]  # identical content, just re-routed


def test_stale_topic_recreate_failure_falls_back_to_threadless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If recreating the topic also fails (`_ensure_topic_thread` returns
    None), the message is still delivered — sent WITHOUT a thread id rather
    than being dropped. No exception escapes `stream_agent_turn`."""
    _stream_topics_settings(monkeypatch)
    # First call resolves 208 (the id that will die); recreate attempt fails
    # (no forum rights, rate-limited, ...) and returns None.
    monkeypatch.setattr(ns, "_ensure_topic_thread", _sequenced_ensure_topic_thread(208, None))

    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "message_thread_id": message_thread_id})
        if message_thread_id is not None:
            raise _FakeTelegramError("Bad Request: message thread not found")

    monkeypatch.setattr(ns, "_send_telegram", _fake)

    ns.stream_agent_turn(actor="Gustave (Developer)", summary="deploy finished")  # must not raise

    assert len(calls) == 2
    assert calls[0]["message_thread_id"] == 208
    assert calls[1]["message_thread_id"] is None  # threadless General fallback
    assert calls[1]["msg"] == calls[0]["msg"]  # content preserved, never dropped


def test_closed_topic_does_not_recreate_sends_to_general(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLOSED (not deleted) topic must NOT trigger a recreate — the
    operator closed it deliberately. The message still reaches the chat's
    General topic instead."""
    _stream_topics_settings(monkeypatch)

    recreate_calls: list[str] = []

    def _ensure(agent_key: str, title: str) -> int:
        recreate_calls.append(agent_key)
        return 208

    monkeypatch.setattr(ns, "_ensure_topic_thread", _ensure)

    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "message_thread_id": message_thread_id})
        if message_thread_id is not None:
            raise _FakeTelegramError("Bad Request: TOPIC_CLOSED")

    monkeypatch.setattr(ns, "_send_telegram", _fake)

    ns.stream_agent_turn(actor="Gustave (Developer)", summary="deploy finished")

    # Only the initial _ensure_topic_thread call (to get 208) -- no second
    # (recreate) call for a closed topic.
    assert recreate_calls == ["developer"]
    assert len(calls) == 2
    assert calls[0]["message_thread_id"] == 208
    assert calls[1]["message_thread_id"] is None  # General, not recreated
    assert calls[1]["msg"] == calls[0]["msg"]


def test_normal_400_error_unchanged_registry_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal 400 (e.g. bad markdown, not a topic problem) keeps the
    EXISTING plain-text-retry-with-same-thread-id behaviour; the registry is
    never touched."""
    _stream_topics_settings(monkeypatch)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)
    monkeypatch.setattr(ns, "_ensure_topic_thread", lambda agent_key, title: 208)

    invalidate_calls: list[str] = []
    monkeypatch.setattr(
        ns, "_invalidate_topic", lambda agent_key: invalidate_calls.append(agent_key)
    )

    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None):
        calls.append({"msg": msg, "message_thread_id": message_thread_id, "parse_mode": parse_mode})
        if parse_mode == "HTML":
            raise _FakeTelegramError("Can't parse entities: unbalanced tag")

    monkeypatch.setattr(ns, "_send_telegram", _fake)

    structured = "## status\nPASS\n## summary\n- **bold** thing done\n"
    ns.stream_agent_turn(actor="Gustave (Developer)", summary=structured)

    assert invalidate_calls == []  # registry never touched for a non-topic 400
    assert len(calls) == 2
    assert calls[0]["message_thread_id"] == 208
    assert calls[1]["message_thread_id"] == 208  # same thread id, unchanged behaviour
    assert calls[1]["parse_mode"] is None


def test_registry_invalidation_persisted_second_call_does_not_reuse_dead_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once invalidated, a fresh `_load_topics()` (simulating a brand-new
    process reading the same on-disk registry) no longer sees the dead id."""
    registry_path = tmp_path / "stream_topics.json"
    monkeypatch.setattr(ns.settings, "stream_topics_registry_path", str(registry_path))
    ns._save_topics({"gustave": 208})

    ns._invalidate_topic("gustave")

    # Simulate a new process: reload from disk from scratch.
    assert ns._load_topics().get("gustave") is None


# ---------------------------------------------------------------------------
# Topic creation is capped, whatever the root cause
# ---------------------------------------------------------------------------


class TestTopicCreationIsBounded:
    """Four topics all correctly named "Gustave (Developer)" appeared in the
    operator's group, and cleaning them up is manual and tedious.

    The naming defects were fixed and tested earlier; this is a DIFFERENT
    failure — the name was right, so the key resolved, and the registry lookup
    still missed four times running. The root cause is not proven.

    So the cap comes first: it bounds the damage without depending on a
    diagnosis. At most ONE creation per role per process; beyond that, stop
    creating and say so. The operator goes from "four topics and it will
    happen again" to "at worst one duplicate, once, and an alert".
    """

    def _telegram(self, monkeypatch, ns, thread_id=900):
        monkeypatch.setattr(ns.settings, "telegram_bot_token", "t", raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", "-100123", raising=False)

        class Resp:
            def json(self):
                return {"ok": True, "result": {"message_thread_id": thread_id}}

        monkeypatch.setattr(ns.requests, "post", lambda *a, **k: Resp())

    def test_a_second_creation_for_the_same_role_is_refused(self, tmp_path, monkeypatch):
        from hivepilot.services import notification_service as ns

        registry = tmp_path / "t.json"
        registry.write_text("{}")
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
        monkeypatch.setattr(ns, "_register_topic", lambda *a, **k: None)  # write never lands
        ns._reset_topic_creation_budget()
        self._telegram(monkeypatch, ns)

        first = ns._ensure_topic_thread("developer", "Gustave (Developer)")
        second = ns._ensure_topic_thread("developer", "Gustave (Developer)")

        assert first == 900
        assert second is None, "a failing registry write must not mean unbounded creation"

    def test_a_different_role_is_unaffected(self, tmp_path, monkeypatch):
        """The cap is per role, not global — one bad key must not silence the
        whole fleet."""
        from hivepilot.services import notification_service as ns

        registry = tmp_path / "t.json"
        registry.write_text("{}")
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
        monkeypatch.setattr(ns, "_register_topic", lambda *a, **k: None)
        ns._reset_topic_creation_budget()
        self._telegram(monkeypatch, ns)

        ns._ensure_topic_thread("developer", "Gustave (Developer)")
        ns._ensure_topic_thread("developer", "Gustave (Developer)")
        other = ns._ensure_topic_thread("ciso", "Hugo (CISO)")

        assert other == 900

    def test_a_registered_topic_is_still_reused_forever(self, tmp_path, monkeypatch):
        """The cap must never break the normal path: a topic in the registry is
        returned every time, without counting against anything."""
        from hivepilot.services import notification_service as ns

        registry = tmp_path / "t.json"
        registry.write_text('{"developer": 330}')
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
        ns._reset_topic_creation_budget()
        # Telegram must be configured: `_ensure_topic_thread` returns early
        # without a token, BEFORE it ever consults the registry. Without this
        # the test passed locally purely because the dev environment happens to
        # carry a token, and failed in CI which does not.
        self._telegram(monkeypatch, ns)

        for _ in range(5):
            assert ns._ensure_topic_thread("developer", "Gustave (Developer)") == 330

    def test_hitting_the_cap_is_logged(self, tmp_path, monkeypatch, caplog):
        """A silent cap is the same disease one level down."""
        from hivepilot.services import notification_service as ns

        registry = tmp_path / "t.json"
        registry.write_text("{}")
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
        monkeypatch.setattr(ns, "_register_topic", lambda *a, **k: None)
        ns._reset_topic_creation_budget()
        self._telegram(monkeypatch, ns)

        ns._ensure_topic_thread("developer", "Gustave (Developer)")
        with caplog.at_level("WARNING"):
            ns._ensure_topic_thread("developer", "Gustave (Developer)")

        assert "topics.creation_capped" in caplog.text


class TestRegisterTopicVerifiesItsWrite:
    """The registry file had not changed since 8 August while topics were
    being created after that date.

    Never proven, but it is the one signal pointing at writes that do not
    land — and a write nobody checks is exactly the failure this codebase
    keeps producing. Verifying turns a silent drift into a loud, single event.
    """

    def test_a_landed_write_is_silent(self, tmp_path, monkeypatch):
        from hivepilot.services import notification_service as ns

        registry = tmp_path / "t.json"
        registry.write_text("{}")
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)

        assert ns._register_topic("developer", 330) is True
        assert '"developer"' in registry.read_text()

    def test_a_write_that_does_not_land_is_reported(self, tmp_path, monkeypatch, caplog):
        from hivepilot.services import notification_service as ns

        registry = tmp_path / "t.json"
        registry.write_text("{}")
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
        # The write silently does nothing — the suspected production shape.
        monkeypatch.setattr(ns, "_write_topics", lambda *a, **k: None)

        with caplog.at_level("WARNING"):
            landed = ns._register_topic("developer", 330)

        assert landed is False
        assert "topics.registry_write_lost" in caplog.text
