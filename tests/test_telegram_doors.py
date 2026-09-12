"""HP-92 — Telegram four doors aligned with Pollen (Inbox/Approvals/Runs/Alerts)."""

from __future__ import annotations

from types import SimpleNamespace

from hivepilot.services.telegram_doors import (
    ALERTS,
    APPROVALS,
    CANONICAL_ROLE_EMOJI,
    DOOR_TITLES,
    INBOX,
    PERSISTENT_DOORS,
    RUNS,
    SYSTEM_EMOJI,
    classify_notification_door,
    concierge_action_prompt,
    concierge_answer_text,
    door_title,
    is_persistent_door,
    is_run_topic_key,
    render_soft_card,
    run_first_message_html,
    run_topic_key,
    run_topic_title,
    slugify,
    soft_card_from_report,
    speaker_html,
    speaker_plain,
)


def test_four_persistent_doors_match_pollen_names() -> None:
    assert PERSISTENT_DOORS == (INBOX, APPROVALS, RUNS, ALERTS)
    assert [DOOR_TITLES[k] for k in PERSISTENT_DOORS] == [
        "Inbox",
        "Approvals",
        "Runs",
        "Alerts",
    ]
    assert "General" not in DOOR_TITLES.values()


def test_role_charte_is_exactly_the_eight_pairs() -> None:
    assert CANONICAL_ROLE_EMOJI == {
        "ceo": "👑",
        "chief_of_staff": "📋",
        "cto": "🧭",
        "developer": "🛠️",
        "reviewer": "🔍",
        "ciso": "🛡️",
        "qa": "🧪",
        "documentation": "📝",
    }


def test_system_uses_bee_not_a_new_role_glyph() -> None:
    assert SYSTEM_EMOJI == "🐝"
    assert SYSTEM_EMOJI not in CANONICAL_ROLE_EMOJI.values()
    assert speaker_plain("HivePilot").startswith("🐝")
    assert "🐝" in speaker_html("HivePilot")


def test_run_topic_title_is_emoji_plus_slug() -> None:
    assert run_topic_key(42) == "run:42"
    assert is_run_topic_key("run:42")
    assert not is_run_topic_key("runs")
    assert not is_run_topic_key("run:")
    assert run_topic_title("developer", "acme-api") == "🛠️ acme-api"
    assert run_topic_title(None, "pipeline docs") == "🐝 pipeline-docs"
    assert "run #42" in run_first_message_html(42, "acme-api")
    assert "acme-api" in run_first_message_html(42, "acme-api")


def test_slugify_stays_short() -> None:
    assert slugify("Acme API!!!") == "acme-api"
    assert len(slugify("x" * 80)) <= 24


def test_classify_failed_degraded_classifier_to_alerts() -> None:
    assert classify_notification_door("❌ run 9 failed") == ALERTS
    assert classify_notification_door("plugin degraded: fallback") == ALERTS
    assert classify_notification_door("classifier down") == ALERTS
    assert classify_notification_door("started on acme") == INBOX


def test_soft_card_is_title_plus_two_meta_lines() -> None:
    card = render_soft_card(
        actor="Gustave (Developer)",
        target="Victor (Reviewer)",
        status="PASS",
        meta="Implementation complete",
    )
    lines = card.split("\n")
    assert len(lines) == 3
    assert "<b>Gustave (Developer)</b>" in lines[0]
    assert "🛠️" in lines[0]
    assert "Victor (Reviewer)" in lines[1]
    assert "PASS" in lines[1]
    assert "Implementation complete" in lines[2]


def test_soft_card_from_report_uses_first_summary_only() -> None:
    report = SimpleNamespace(
        status="PASS",
        summary=["Implementation complete", "Tests passing", "extra"],
        next_handoff="review",
        confidence="high",
        links=["/vault/artifact.md"],
    )
    card = soft_card_from_report(actor="Developer", target="Reviewer", report=report)
    assert card.count("\n") == 2
    assert "Implementation complete" in card
    assert "Tests passing" not in card
    assert "artifact.md" not in card


def test_answer_never_looks_like_an_action_prompt() -> None:
    text = concierge_answer_text("Nothing is running right now.")
    assert text.startswith("🐝")
    assert "Confirm" not in text
    assert concierge_answer_text("🐝 already").startswith("🐝 already")


def test_action_prompt_is_soft_and_explicit() -> None:
    text = concierge_action_prompt("ask developer to work on acme: fix")
    assert text.startswith("🐝 Confirm")
    assert "Yes runs it" in text
    assert "No cancels" in text


def test_notify_telegram_routes_failed_to_alerts(monkeypatch) -> None:
    from hivepilot.services import notification_service as ns

    sent: list[dict] = []

    def fake_send(message, chat_id=None, message_thread_id=None, parse_mode=None):
        sent.append({"msg": message, "chat_id": chat_id, "thread": message_thread_id})

    monkeypatch.setattr(ns.settings, "telegram_stream_topics", True, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", -100111, raising=False)
    monkeypatch.setattr(ns, "_send_telegram", fake_send)
    monkeypatch.setattr(ns, "door_thread", lambda door: 10 if door == ALERTS else 11)

    ns._notify_telegram("❌ run 9 failed on acme")
    assert sent == [{"msg": "❌ run 9 failed on acme", "chat_id": -100111, "thread": 10}]

    sent.clear()
    ns._notify_telegram("hello from concierge")
    assert sent == [{"msg": "hello from concierge", "chat_id": -100111, "thread": 11}]


def test_door_title_helpers() -> None:
    assert door_title("inbox") == "Inbox"
    assert door_title("developer") is None
    assert is_persistent_door("alerts")
    assert not is_persistent_door("developer")
