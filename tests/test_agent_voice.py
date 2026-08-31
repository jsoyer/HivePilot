"""Tests for the runner-backed agent voice (HP-49 slice 2).

The `capture` seam lets us drive the whole wiring — role resolution → runner
definition → reply — without a live model.
"""

from __future__ import annotations

import pytest

from hivepilot.services import (
    agent_voice,
    async_run_service,
    delegation,
    spaces_responder,
    state_service,
)


@pytest.fixture(autouse=True)
def _reset_generators():
    spaces_responder.register_reply_generator(None)
    delegation.register_subagent_executor(None)
    yield
    spaces_responder.register_reply_generator(None)
    delegation.register_subagent_executor(None)


def _capture_ok(runner_def, payload):
    # The runner_def is built from the role — assert the wiring reached us.
    return f"  reply from {runner_def.name}  "


class TestBuildReply:
    def test_voices_a_known_role_via_capture(self) -> None:
        reply = agent_voice.build_reply(
            {}, "ceo", [{"sender_type": "human", "body": "hi"}], capture=_capture_ok
        )
        assert reply == "reply from voice:ceo"  # stripped, role-specific

    def test_unknown_role_is_none(self) -> None:
        assert agent_voice.build_reply({}, "no_such_role", [], capture=_capture_ok) is None

    def test_capture_failure_is_fail_closed(self) -> None:
        def _boom(runner_def, payload):
            raise RuntimeError("model unreachable")

        assert agent_voice.build_reply({}, "ceo", [], capture=_boom) is None

    def test_blank_capture_yields_none(self) -> None:
        assert agent_voice.build_reply({}, "ceo", [], capture=lambda rd, pl: "   ") is None


class TestRegisterWiring:
    def test_register_makes_a_role_reply_in_a_space(self) -> None:
        agent_voice.register(capture=_capture_ok)

        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        state_service.add_space_message(sid, "human", "salut")
        spaces_responder.dispatch_reply(sid)
        assert async_run_service.wait_until_idle(5.0)

        msgs = state_service.list_space_messages(sid)
        assert msgs[-1]["sender_type"] == "role"
        assert msgs[-1]["sender_id"] == "ceo"
        assert msgs[-1]["body"] == "reply from voice:ceo"

    def test_register_wires_the_delegation_subagent(self) -> None:
        agent_voice.register(capture=_capture_ok)
        assert delegation.run_subagent("ceo", "summarise this") == "reply from voice:ceo"
