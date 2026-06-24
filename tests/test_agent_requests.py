"""Tests for Tier-2 on-demand orchestrator-mediated agent-to-agent requests."""

from __future__ import annotations

from unittest.mock import patch

from hivepilot.services import notification_service as ns
from hivepilot.services import state_service
from hivepilot.services.agent_report import parse_agent_requests
from hivepilot.services.interaction_service import log_request_interaction

# ---------------------------------------------------------------------------
# C2: parse_agent_requests — pure function
# ---------------------------------------------------------------------------


class TestParseAgentRequests:
    def test_parses_single_request_emdash(self):
        text = "request: CTO — What is the current database schema version?"
        result = parse_agent_requests(text)
        assert result == [("CTO", "What is the current database schema version?")]

    def test_parses_single_request_double_dash(self):
        text = "request: CISO -- Are there open CVEs in our dependencies?"
        result = parse_agent_requests(text)
        assert result == [("CISO", "Are there open CVEs in our dependencies?")]

    def test_parses_uppercase_REQUEST(self):
        text = "REQUEST: Developer — Which API endpoints are affected?"
        result = parse_agent_requests(text)
        assert result == [("Developer", "Which API endpoints are affected?")]

    def test_ignores_none_value(self):
        text = "request: none"
        result = parse_agent_requests(text)
        assert result == []

    def test_ignores_malformed_no_separator(self):
        text = "request: some question without target"
        result = parse_agent_requests(text)
        assert result == []

    def test_ignores_empty_question(self):
        text = "request: CTO — "
        result = parse_agent_requests(text)
        assert result == []

    def test_parses_multiple_requests(self):
        text = (
            "request: CTO — What model is used for code generation?\n"
            "request: CISO — Is the API token rotated quarterly?\n"
        )
        result = parse_agent_requests(text)
        assert len(result) == 2
        assert result[0] == ("CTO", "What model is used for code generation?")
        assert result[1] == ("CISO", "Is the API token rotated quarterly?")

    def test_ignores_non_request_lines(self):
        text = (
            "status: PASS\n"
            "summary: All good\n"
            "challenge: CTO — timeline too aggressive\n"
            "request: CISO — Any open vulnerabilities?\n"
        )
        result = parse_agent_requests(text)
        assert result == [("CISO", "Any open vulnerabilities?")]

    def test_empty_text(self):
        assert parse_agent_requests("") == []


# ---------------------------------------------------------------------------
# C4: stream_agent_request / stream_agent_answer — notification streaming
# ---------------------------------------------------------------------------


class TestStreamAgentRequest:
    def test_stream_agent_request_emits_question_turn(self, monkeypatch):
        monkeypatch.setattr(ns.settings, "telegram_stream_live", True)
        monkeypatch.setattr(ns.settings, "telegram_stream_rich", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", "test_chat")
        captured = []

        def fake_send(message, **kwargs):
            captured.append(message)

        monkeypatch.setattr(ns, "_send_telegram", fake_send)
        ns.stream_agent_request(
            requester="CTO", target="CISO", question="Are dependencies patched?"
        )
        assert len(captured) == 1
        assert "❓" in captured[0]

    def test_stream_agent_answer_emits_answer_turn(self, monkeypatch):
        monkeypatch.setattr(ns.settings, "telegram_stream_live", True)
        monkeypatch.setattr(ns.settings, "telegram_stream_rich", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", "test_chat")
        captured = []

        def fake_send(message, **kwargs):
            captured.append(message)

        monkeypatch.setattr(ns, "_send_telegram", fake_send)
        ns.stream_agent_answer(target="CISO", requester="CTO", answer_excerpt="Yes, all patched.")
        assert len(captured) == 1
        assert "↩️" in captured[0]


# ---------------------------------------------------------------------------
# C5d: log_request_interaction
# ---------------------------------------------------------------------------


class TestLogRequestInteraction:
    def test_log_request_records_action(self):
        recorded = []

        def fake_record(**kwargs):
            recorded.append(dict(kwargs))
            return 0

        with patch.object(state_service, "record_interaction", side_effect=fake_record):
            log_request_interaction(actor="CTO", target="CISO", question="Open CVEs?")
        assert recorded[0]["action"] == "request"
        assert recorded[0]["actor"] == "CTO"

    def test_log_answer_records_answer_action(self):
        recorded = []

        def fake_record(**kwargs):
            recorded.append(dict(kwargs))
            return 0

        with patch.object(state_service, "record_interaction", side_effect=fake_record):
            log_request_interaction(
                actor="CISO", target="CTO", question="[ANSWER] No open CVEs found."
            )
        assert recorded[0]["action"] == "answer"
