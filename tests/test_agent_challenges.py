"""Tests for inter-agent challenge surfacing (Part A — visible-challenges feature).

Covers:
- parse_agent_report: em-dash format
- parse_agent_report: double-dash format
- parse_agent_report: fallback to rejection_notice when challenge: absent
- parse_agent_report: none value → challenge is None
- stream_challenge: emits a ⚔️ turn via _send_telegram
- log_challenge_interaction: records action="challenge" in state_service
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hivepilot.services import notification_service as ns
from hivepilot.services.agent_report import parse_agent_report
from hivepilot.services.interaction_service import log_challenge_interaction

# ---------------------------------------------------------------------------
# parse_agent_report — challenge field
# ---------------------------------------------------------------------------


def test_parse_challenge_em_dash() -> None:
    """challenge: with em-dash separator is parsed into ChallengeInfo."""
    text = (
        "status: PASS\n"
        "summary:\n- all good\n"
        "challenge: Chief of Staff — timeline is unrealistic given current backlog\n"
    )
    report = parse_agent_report(text)
    assert report.challenge is not None
    assert report.challenge.target == "Chief of Staff"
    assert "unrealistic" in report.challenge.point


def test_parse_challenge_double_dash() -> None:
    """challenge: with double-dash separator is also parsed correctly."""
    text = "status: PASS\nsummary:\n- ok\nchallenge: CEO -- scope conflicts with GDPR constraints\n"
    report = parse_agent_report(text)
    assert report.challenge is not None
    assert report.challenge.target == "CEO"
    assert "GDPR" in report.challenge.point


def test_parse_challenge_fallback_to_rejection_notice() -> None:
    """When challenge: is absent, rejection_notice: is used as the challenge point."""
    text = (
        "status: BLOCKED\nsummary:\n- blocked\nrejection_notice: CTO spec missing auth contract\n"
    )
    report = parse_agent_report(text)
    # rejection_notice without a target name → target is empty, point is the value
    assert report.challenge is not None
    assert "auth contract" in report.challenge.point


def test_parse_challenge_none_value() -> None:
    """challenge: none (case-insensitive) results in challenge=None."""
    text = "status: PASS\nsummary:\n- all clear\nchallenge: none\n"
    report = parse_agent_report(text)
    assert report.challenge is None


# ---------------------------------------------------------------------------
# stream_challenge — Telegram emission
# ---------------------------------------------------------------------------


def test_stream_challenge_emits_sword_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_challenge sends a message containing the ⚔️ icon."""
    monkeypatch.setattr(ns.settings, "telegram_stream_live", True)
    monkeypatch.setattr(ns.settings, "telegram_stream_rich", False)
    monkeypatch.setattr(ns.settings, "telegram_stream_topics", False)
    monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", "test_chat")

    captured: list[str] = []

    def fake_send(message: str, **kwargs: object) -> None:
        captured.append(message)

    monkeypatch.setattr(ns, "_send_telegram", fake_send)

    ns.stream_challenge(actor="CTO", target="Chief of Staff", point="timeline is wrong")

    assert len(captured) == 1
    assert "⚔️" in captured[0]


# ---------------------------------------------------------------------------
# log_challenge_interaction — state_service recording
# ---------------------------------------------------------------------------


def test_log_challenge_interaction_records_action() -> None:
    """log_challenge_interaction records action='challenge' via state_service."""
    from hivepilot.services import state_service

    recorded: list[dict] = []

    def fake_record(**kwargs: object) -> int:
        recorded.append(dict(kwargs))
        return 0

    with patch.object(state_service, "record_interaction", side_effect=fake_record):
        log_challenge_interaction(actor="CTO", target="CEO", point="missing NFRs")

    assert len(recorded) == 1
    assert recorded[0]["action"] == "challenge"
    assert recorded[0]["actor"] == "CTO"
    assert recorded[0]["target"] == "CEO"
    assert "NFRs" in recorded[0]["summary"]
