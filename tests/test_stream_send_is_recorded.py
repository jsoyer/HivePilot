"""A delivered stream message must record that it was delivered.

Failures logged; successes did not. So an empty log was compatible with both
"the roles are speaking" and "the roles are mute", and I read that silence as
absence three times in one session -- including once while telling the operator
the roles were not speaking, when the wiring was in fact fine.

This is the house defect in miniature: work that happens and records nothing is
indistinguishable from work that never happened.
"""

from __future__ import annotations

import pytest

from hivepilot.services import notification_service as ns


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ns, "_send_telegram", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        ns.logger, "info", lambda event, **kw: calls.append({"event": event, **kw}), raising=False
    )
    return calls


class TestASuccessfulSendIsRecorded:
    def test_it_logs_the_thread_it_reached(self, sent):
        ns._send_one_chunk(
            "hello",
            chat_id="-100x",
            message_thread_id=330,
            parse_mode=None,
            agent_key="developer",
            topic_title="Gustave (Developer)",
        )

        record = next(c for c in sent if c["event"] == "stream.telegram.sent")
        assert record["message_thread_id"] == 330
        assert record["agent_key"] == "developer"

    def test_it_records_a_size_not_the_body(self, sent):
        """The body is agent output and can echo a resolved secret; the length
        is what an operator needs in order to see that delivery happened."""
        ns._send_one_chunk(
            "sk-should-never-be-logged-here",
            chat_id="-100x",
            message_thread_id=330,
            parse_mode=None,
            agent_key="developer",
            topic_title=None,
        )

        record = next(c for c in sent if c["event"] == "stream.telegram.sent")
        assert record["chars"] == len("sk-should-never-be-logged-here")
        assert "sk-should-never-be-logged-here" not in str(record)
