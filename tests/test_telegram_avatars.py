"""HP-16 — per-role Telegram avatars (Unicode fallback + optional custom emoji)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hivepilot.services import notification_service as ns
from hivepilot.services import telegram_avatars as tgav

CANONICAL = (
    "ceo",
    "chief_of_staff",
    "cto",
    "developer",
    "reviewer",
    "ciso",
    "qa",
    "documentation",
)


@pytest.fixture(autouse=True)
def _reset_avatar_cache() -> None:
    tgav.reset_cache()
    yield
    tgav.reset_cache()


def test_canonical_map_covers_eight_roles() -> None:
    assert set(tgav.CANONICAL_ROLE_EMOJI) == set(CANONICAL)
    assert all(tgav.CANONICAL_ROLE_EMOJI[key] for key in CANONICAL)


def test_fallback_unknown_role_is_empty() -> None:
    assert tgav.fallback_emoji("intern") == ""
    assert tgav.fallback_emoji(None) == ""
    assert tgav.html_mark(None) == ""
    assert tgav.plain_mark("not-a-role") == ""


def test_html_mark_unicode_when_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {})
    mark = tgav.html_mark("ceo")
    assert mark == "👑 "
    assert "tg-emoji" not in mark


def test_html_mark_wraps_configured_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {"ceo": "5368324170671202286"})
    monkeypatch.setattr(tgav.settings, "telegram_custom_emoji", True, raising=False)
    assert tgav.html_mark("ceo") == ('<tg-emoji emoji-id="5368324170671202286">👑</tg-emoji> ')


def test_html_mark_disabled_flag_never_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {"ceo": "5368324170671202286"})
    monkeypatch.setattr(tgav.settings, "telegram_custom_emoji", False, raising=False)
    assert tgav.html_mark("ceo") == "👑 "
    assert tgav.custom_emoji_id("ceo") is None
    assert tgav.custom_emoji_entities("ceo") is None


def test_custom_emoji_entities_utf16_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {"ceo": "99"})
    monkeypatch.setattr(tgav.settings, "telegram_custom_emoji", True, raising=False)
    prefix = "🗣 (hand-off) "
    entities = tgav.custom_emoji_entities("ceo", prefix=prefix)
    assert entities == [
        {
            "type": "custom_emoji",
            "offset": tgav.utf16_len(prefix),
            "length": tgav.utf16_len("👑"),
            "custom_emoji_id": "99",
        }
    ]
    assert entities[0]["length"] == 2  # U+1F451 is a UTF-16 surrogate pair


def test_parse_avatar_ids_flat_and_nested() -> None:
    assert tgav.parse_avatar_ids({"ceo": "12", "sticker_set": "x_by_bot"}) == {"ceo": "12"}
    assert tgav.parse_avatar_ids({"avatars": {"cto": {"custom_emoji_id": "34"}, "qa": ""}}) == {
        "cto": "34"
    }
    assert tgav.parse_avatar_ids(["nope"]) == {}
    assert tgav.parse_avatar_ids({"developer": "not-digits"}) == {}


def test_role_key_from_actor_matches_display_and_title() -> None:
    assert tgav.role_key_from_actor("Blaise (CTO)") == "cto"
    assert tgav.role_key_from_actor("Developer") == "developer"
    assert tgav.role_key_from_actor("some pipeline stage") is None


def test_load_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "telegram_avatars.yaml"
    path.write_text(yaml.dump({"avatars": {"reviewer": "777"}}), encoding="utf-8")
    monkeypatch.setattr(tgav.settings, "telegram_avatars_file", Path("telegram_avatars.yaml"))
    monkeypatch.setattr(tgav.settings, "resolve_config_path", lambda p: path)
    assert tgav.load_custom_emoji_ids() == {"reviewer": "777"}
    # cache hit
    path.write_text(yaml.dump({"avatars": {"reviewer": "888"}}), encoding="utf-8")
    # mtime usually changes; if it does not, still acceptable — force reset
    tgav.reset_cache()
    assert tgav.load_custom_emoji_ids() == {"reviewer": "888"}


def test_validate_unknown_role_and_bad_id(tmp_path: Path) -> None:
    path = tmp_path / "telegram_avatars.yaml"
    path.write_text(
        yaml.dump({"avatars": {"not_a_role": "1", "ceo": "abc"}}),
        encoding="utf-8",
    )
    problems = tgav.validate_telegram_avatars_file(path)
    assert any("unknown role 'not_a_role'" in p for p in problems)
    assert any("ceo" in p and "digits" in p for p in problems)


def test_validate_missing_file_is_ok(tmp_path: Path) -> None:
    assert tgav.validate_telegram_avatars_file(tmp_path / "nope.yaml") == []


def test_rich_card_prefixes_fallback_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None, **_k):
        calls.append({"msg": msg, "parse_mode": parse_mode})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {})
    monkeypatch.setattr(tgav.settings, "telegram_custom_emoji", True, raising=False)

    ns.stream_agent_turn(
        actor="Blaise (CTO)",
        summary="## status\nPASS\n## summary\n- done\n",
    )
    assert calls
    assert "🧭" in calls[0]["msg"]
    assert "tg-emoji" not in calls[0]["msg"]
    assert "<b>Blaise (CTO)</b>" in calls[0]["msg"]


def test_rich_card_uses_tg_emoji_when_id_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None, **_k):
        calls.append({"msg": msg, "parse_mode": parse_mode})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", True, raising=False)
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {"cto": "42"})
    monkeypatch.setattr(tgav.settings, "telegram_custom_emoji", True, raising=False)

    ns.stream_agent_turn(
        actor="Blaise (CTO)",
        summary="## status\nPASS\n## summary\n- done\n",
    )
    assert '<tg-emoji emoji-id="42">🧭</tg-emoji>' in calls[0]["msg"]
    assert calls[0]["parse_mode"] == "HTML"


def test_plain_path_attaches_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None, entities=None):
        calls.append({"msg": msg, "parse_mode": parse_mode, "entities": entities})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {"cto": "42"})
    monkeypatch.setattr(tgav.settings, "telegram_custom_emoji", True, raising=False)

    ns.stream_agent_turn(actor="Blaise (CTO)", summary="short")
    assert calls[0]["parse_mode"] is None
    assert calls[0]["entities"][0]["type"] == "custom_emoji"
    assert calls[0]["entities"][0]["custom_emoji_id"] == "42"
    assert "🧭" in calls[0]["msg"]
    emoji_start = calls[0]["msg"].index("🧭")
    assert calls[0]["entities"][0]["offset"] == tgav.utf16_len(calls[0]["msg"][:emoji_start])


def test_unknown_actor_keeps_stream_icon_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None, **_k):
        calls.append({"msg": msg})

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)

    ns.stream_agent_turn(actor="refresh", summary="heartbeat")
    msg = calls[0]["msg"]
    assert "🗣" in msg
    for glyph in tgav.CANONICAL_ROLE_EMOJI.values():
        assert glyph not in msg


def test_send_telegram_entities_omitted_when_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        r = type("R", (), {"ok": True})()
        return r

    monkeypatch.setattr(ns.requests, "post", fake_post)
    monkeypatch.setattr(ns.settings, "telegram_bot_token", "tok", raising=False)
    monkeypatch.setattr(ns.settings, "telegram_notification_chat_id", 1, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_allowed_chat_ids", [], raising=False)

    ns._send_telegram(
        "hi",
        chat_id=1,
        parse_mode="HTML",
        entities=[{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "1"}],
    )
    assert captured["payload"]["parse_mode"] == "HTML"
    assert "entities" not in captured["payload"]


def test_send_telegram_entities_in_payload_when_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        r = type("R", (), {"ok": True})()
        return r

    monkeypatch.setattr(ns.requests, "post", fake_post)
    monkeypatch.setattr(ns.settings, "telegram_bot_token", "tok", raising=False)
    monkeypatch.setattr(ns.settings, "telegram_notification_chat_id", 1, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_allowed_chat_ids", [], raising=False)

    entity = {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "1"}
    ns._send_telegram("👑 hi", chat_id=1, parse_mode=None, entities=[entity])
    assert "parse_mode" not in captured["payload"]
    assert captured["payload"]["entities"] == [entity]


def test_plain_entities_retry_without_custom_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[dict] = []

    def _fake(msg, chat_id=None, message_thread_id=None, parse_mode=None, entities=None):
        attempts.append({"entities": entities, "parse_mode": parse_mode})
        if entities:
            raise ns.TelegramSendError(
                status_code=400,
                description="custom emoji not allowed",
                chat_id=chat_id,
                message_thread_id=message_thread_id,
            )

    monkeypatch.setattr(ns, "_send_telegram", _fake)
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
    monkeypatch.setattr(tgav, "load_custom_emoji_ids", lambda: {"cto": "42"})
    monkeypatch.setattr(tgav.settings, "telegram_custom_emoji", True, raising=False)

    ns.stream_agent_turn(actor="Blaise (CTO)", summary="short")
    assert len(attempts) == 2
    assert attempts[0]["entities"] is not None
    assert attempts[1]["entities"] is None
