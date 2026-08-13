"""The agents' exchanges, readable as conversations rather than as a log.

Every stage's output is already persisted as an `interactions` row carrying the
role key. Nothing ever presented them as what they are: one thread per run, one
voice per role. The operator asked whether Pollen could show the conversations
between agents, and the answer was that the data had been there all along with
no surface on it.

Replying is deliberately NOT a message to a running agent -- by the time a
thread is readable its agents have exited. A reply appends to the role's
corrections file, which is the path that already feeds the next run of that
role. Anything else would be a chat window that changes nothing.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    from hivepilot.services import state_service

    first = state_service.record_run_start(project="noxys", task="noxys")
    for actor, role, summary in (
        ("Aliénor (CEO)", "ceo", "Objective: ship the metrics table."),
        ("Gustave (Developer)", "developer", "Implemented mdstat with tests."),
        (
            "Victor (Reviewer)",
            "reviewer",
            "status: REQUEST_CHANGES\nThe grant path never checks isAdmin.",
        ),
    ):
        state_service.record_interaction(
            actor=actor,
            action="completed stage",
            target=None,
            summary=summary,
            run_id=first,
            metadata={"pipeline": "noxys", "role": role},
        )

    second = state_service.record_run_start(project="forage", task="forage")
    state_service.record_interaction(
        actor="Gustave (Developer)",
        action="completed stage",
        target=None,
        summary="Implemented the CLI.",
        run_id=second,
        metadata={"pipeline": "forage", "role": "developer"},
    )
    return first, second


class TestTheThreadReadsAsAConversation:
    def test_messages_come_back_in_order(self, runs):
        from hivepilot.services import conversations_service

        first, _ = runs
        thread = conversations_service.thread(first)

        assert [m.role for m in thread.messages] == ["ceo", "developer", "reviewer"]

    def test_each_message_carries_its_speaker_and_text(self, runs):
        from hivepilot.services import conversations_service

        first, _ = runs
        message = conversations_service.thread(first).messages[-1]

        assert message.actor == "Victor (Reviewer)"
        assert message.role == "reviewer"
        assert "never checks isAdmin" in message.body

    def test_an_unknown_run_is_an_empty_thread_not_an_error(self, runs):
        """A Pollen view must not 500 because a run id went stale."""
        from hivepilot.services import conversations_service

        assert conversations_service.thread(999_999).messages == []


class TestTheRunListIsUsable:
    def test_it_lists_runs_that_actually_have_messages(self, runs):
        from hivepilot.services import conversations_service

        listed = conversations_service.recent_runs(limit=10)

        assert {r.run_id for r in listed} == set(runs)

    def test_it_reports_who_spoke_and_how_many_times(self, runs):
        from hivepilot.services import conversations_service

        first, _ = runs
        entry = next(r for r in conversations_service.recent_runs(10) if r.run_id == first)

        assert entry.message_count == 3
        assert set(entry.roles) == {"ceo", "developer", "reviewer"}

    def test_newest_first(self, runs):
        from hivepilot.services import conversations_service

        first, second = runs
        listed = conversations_service.recent_runs(10)

        assert listed[0].run_id == second

    def test_a_run_with_no_interactions_is_not_listed(self, tmp_path, monkeypatch):
        """An empty run in the list is a dead end the operator clicks once."""
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        from hivepilot.services import conversations_service, state_service

        state_service.record_run_start(project="p", task="t")

        assert conversations_service.recent_runs(10) == []


class TestASecretNeverReachesTheBrowser:
    def test_a_registered_value_is_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        from hivepilot.services import conversations_service, state_service
        from hivepilot.services.config_provenance import register_secret_value

        register_secret_value("sk-conversations-must-not-leak-8823")
        run_id = state_service.record_run_start(project="p", task="t")
        state_service.record_interaction(
            actor="Gustave (Developer)",
            action="completed stage",
            target=None,
            summary="the token sk-conversations-must-not-leak-8823 is hardcoded",
            run_id=run_id,
            metadata={"role": "developer"},
        )

        body = conversations_service.thread(run_id).messages[0].body

        assert "sk-conversations-must-not-leak-8823" not in body


class TestReplyingFeedsTheNextRun:
    def test_a_reply_lands_in_the_role_corrections(self, runs, monkeypatch, tmp_path):
        """Not a chat window. A reply that changed nothing would be worse than
        no reply, because it would look like it had."""
        from hivepilot.services import conversations_service

        captured: dict[str, str] = {}

        def fake_append(role_key, text, *, commit=True, author="agent"):
            captured.update(role=role_key, text=text, author=author)
            return tmp_path / f"{role_key}.md"

        monkeypatch.setattr(conversations_service, "_append_correction", fake_append, raising=False)

        conversations_service.reply(role="reviewer", text="Check isAdmin on every grant path.")

        assert captured["role"] == "reviewer"
        assert "isAdmin" in captured["text"]

    def test_the_reply_is_attributed_to_the_operator(self, runs, monkeypatch, tmp_path):
        """An operator instruction must not be recorded as the agent's own
        self-correction -- that is how a corpus starts believing its own
        output."""
        from hivepilot.services import conversations_service

        captured: dict[str, str] = {}
        monkeypatch.setattr(
            conversations_service,
            "_append_correction",
            lambda role_key, text, *, commit=True, author="agent": (
                captured.update(author=author) or tmp_path / "x.md"
            ),
            raising=False,
        )

        conversations_service.reply(role="reviewer", text="do the thing")

        assert captured["author"] == "operator"

    def test_an_empty_reply_is_refused(self, runs):
        from hivepilot.services import conversations_service

        with pytest.raises(ValueError):
            conversations_service.reply(role="reviewer", text="   ")

    def test_an_unknown_role_is_refused(self, runs):
        """Select, never invent: writing a corrections file for a role that
        does not exist creates a file nothing will ever read."""
        from hivepilot.services import conversations_service

        with pytest.raises(ValueError):
            conversations_service.reply(role="wizard", text="hello")
