"""Tests for the delegation primitives (HP-48, Cycle 1 · P2).

The collaboration vocabulary the orchestrator uses: run_subagent (ephemeral),
spawn_peer (background run), message_role (async into a space), and handoff
(bounded). Execution is injected; hop-limit + wiring are tested here.
"""

from __future__ import annotations

import pytest

from hivepilot.services import (
    async_run_service,
    delegation,
    spaces_responder,
    state_service,
)


@pytest.fixture(autouse=True)
def _reset_executors():
    delegation.register_subagent_executor(None)
    delegation.register_peer_executor(None)
    spaces_responder.register_reply_generator(None)
    yield
    delegation.register_subagent_executor(None)
    delegation.register_peer_executor(None)
    spaces_responder.register_reply_generator(None)


class TestRunSubagent:
    def test_none_without_executor(self) -> None:
        assert delegation.run_subagent("ceo", "do a thing") is None

    def test_returns_executor_output(self) -> None:
        delegation.register_subagent_executor(lambda role, prompt: f"{role} did: {prompt}")
        assert delegation.run_subagent("ceo", "summarise") == "ceo did: summarise"

    def test_failure_is_isolated(self) -> None:
        def _boom(role, prompt):
            raise RuntimeError("nope")

        delegation.register_subagent_executor(_boom)
        assert delegation.run_subagent("ceo", "x") is None  # never raises


class TestMessageRole:
    def test_posts_and_triggers_reply(self) -> None:
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        spaces_responder.register_reply_generator(lambda space, role, thread: f"{role} ack")

        delegation.message_role(sid, "salut", sender_type="human")
        assert async_run_service.wait_until_idle(5.0)

        bodies = [(m["sender_type"], m["body"]) for m in state_service.list_space_messages(sid)]
        assert bodies == [("human", "salut"), ("role", "ceo ack")]


class TestSpawnPeer:
    def test_creates_a_run(self) -> None:
        run_id = delegation.spawn_peer("proj", "task", "ceo", tenant="acme")
        row = state_service.get_run(run_id)
        assert row is not None and row["status"] == "running"

    def test_dispatches_peer_executor(self) -> None:
        seen = {}
        delegation.register_peer_executor(
            lambda run_id, role: seen.update(run_id=run_id, role=role)
        )
        run_id = delegation.spawn_peer("proj", "task", "cto")
        assert async_run_service.wait_until_idle(5.0)
        assert seen == {"run_id": run_id, "role": "cto"}


class TestHandoff:
    def test_bounded_stops_at_the_hop_limit(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "delegation_max_hops", 2)
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        spaces_responder.register_reply_generator(lambda space, role, thread: "should not run")

        result = delegation.handoff(sid, "ceo", "cto", "à toi", hops=2)
        assert async_run_service.wait_until_idle(2.0)

        assert result["status"] == "limit_reached"
        msgs = state_service.list_space_messages(sid)
        assert msgs[-1]["sender_type"] == "system"
        assert "limit reached" in msgs[-1]["body"].lower()
        # no role reply was dispatched
        assert not any(m["sender_type"] == "role" for m in msgs)

    def test_within_limit_adds_target_and_dispatches_only_it(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "delegation_max_hops", 4)
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        spaces_responder.register_reply_generator(lambda space, role, thread: f"{role} on it")

        result = delegation.handoff(sid, "ceo", "cto", "prends la suite", hops=0)
        assert async_run_service.wait_until_idle(5.0)

        assert result == {"status": "dispatched", "to": "cto", "hops": 1}
        # cto was brought into the room
        parts = state_service.get_space(sid)["participants"]
        assert {"type": "role", "id": "cto"} in parts
        # the handoff message carries a trace, and ONLY cto replied
        msgs = state_service.list_space_messages(sid)
        handoff_msg = next(m for m in msgs if m["sender_id"] == "ceo")
        assert handoff_msg["actions"] == [{"label": "handoff → cto"}]
        role_replies = [
            m["sender_id"] for m in msgs if m["sender_type"] == "role" and m["sender_id"] != "ceo"
        ]
        assert role_replies == ["cto"]
