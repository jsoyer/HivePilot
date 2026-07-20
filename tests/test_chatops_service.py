"""Tests for hivepilot/services/chatops_service.py — the shared command-dispatch
layer used by the webhook-driven ChatOps handlers (`handle_slack`/`handle_discord`/
`handle_telegram`) and, as of Phase 23e, `handle_signal` (the pull-only Signal bot's
receive loop calls this directly, since Signal has no inbound webhook).

Only covers what Phase 23e touched: the new `handle_signal` entry point and the new
`status` branch in `_dispatch` (added so `/status` has parity across all four chat
platforms). Pre-existing handlers (`handle_slack`/`handle_discord`/`handle_telegram`)
already have indirect coverage via `tests/test_pentest.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services import chatops_service


@pytest.fixture(autouse=True)
def _chatops_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_dispatch` gates every command behind `_verify()`, which requires
    `settings.chatops_token` to resolve to a token with sufficient role. Stub
    `_verify` directly (mirrors test_pentest.py's approach) so these tests
    focus on command routing, not token resolution."""
    monkeypatch.setattr(chatops_service, "_verify", lambda required: None)


class TestHandleSignal:
    def test_routes_run_command_to_orchestrator(self) -> None:
        orch = MagicMock()
        orch.run_task.return_value = []
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            result = chatops_service.handle_signal({"text": "/run acme deploy do it"})
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["project_names"] == ["acme"]
        assert orch.run_task.call_args.kwargs["task_name"] == "deploy"
        assert "deploy" in result

    def test_leading_slash_is_optional(self) -> None:
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            result = chatops_service.handle_signal({"text": "approvals"})
        assert "No pending approvals." in result or "run_id" in result

    def test_bare_approve_form_routes_to_run_approved(self) -> None:
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            result = chatops_service.handle_signal({"text": "approve 42"})
        orch.run_approved.assert_called_once_with(
            run_id=42, approve=True, approver="signal", reason=None
        )
        assert "42" in result

    def test_bare_deny_form_routes_to_run_approved(self) -> None:
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            chatops_service.handle_signal({"text": "deny 42"})
        orch.run_approved.assert_called_once_with(
            run_id=42, approve=False, approver="signal", reason="Denied via Signal"
        )

    def test_status_command_lists_recent_runs(self) -> None:
        runs = [{"status": "success", "project": "acme", "task": "deploy", "started_at": "t1"}]
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=MagicMock()),
            patch("hivepilot.services.state_service.list_recent_runs", return_value=runs),
        ):
            result = chatops_service.handle_signal({"text": "/status"})
        assert "acme" in result and "deploy" in result

    def test_status_command_no_runs(self) -> None:
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=MagicMock()),
            patch("hivepilot.services.state_service.list_recent_runs", return_value=[]),
        ):
            result = chatops_service.handle_signal({"text": "/status"})
        assert result == "No recent runs."

    def test_empty_text_returns_unknown(self) -> None:
        assert chatops_service.handle_signal({"text": ""}) == "Unknown command"

    def test_unknown_command_returned_verbatim(self) -> None:
        with patch.object(chatops_service, "_get_orchestrator", return_value=MagicMock()):
            result = chatops_service.handle_signal({"text": "/xyzzy secret"})
        assert "Unknown command" in result


class TestDispatchStatusBranch:
    """`_dispatch("status", ...)` is exercised directly (not just via
    handle_signal) since Slack/Discord/Telegram's own bot implementations
    duplicate this logic locally rather than calling `_dispatch` — this is
    the one place `status` now lives as shared, reusable dispatch logic."""

    def test_status_requires_run_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(chatops_service, "_verify", lambda required: calls.append(required))
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=MagicMock()),
            patch("hivepilot.services.state_service.list_recent_runs", return_value=[]),
        ):
            chatops_service._dispatch("status", [], source="signal")
        assert calls == ["run"]


class TestDispatchConciergeOff:
    """`chatops_concierge_enabled=False` (the default) must be byte-identical
    to pre-concierge behaviour: the fallback still returns "Unknown command:
    ..." and `concierge_service.route` is never imported/called."""

    def test_unknown_command_byte_identical_when_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", False)
        with patch("hivepilot.services.concierge_service.route") as mock_route:
            result = chatops_service._dispatch("foo", ["bar"], source="signal")
        assert result == "Unknown command: foo"
        mock_route.assert_not_called()

    def test_yes_no_not_special_cased_when_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", False)
        result = chatops_service._dispatch("yes", ["sometoken"], source="signal")
        assert result == "Unknown command: yes"


class TestDispatchConciergeOn:
    """`chatops_concierge_enabled=True` — free text that doesn't match a known
    command is classified via `concierge_service.route`."""

    def test_answer_kind_returned_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(kind="answer", answer_text="Hello!")
        with patch(
            "hivepilot.services.concierge_service.route", return_value=decision
        ) as mock_route:
            result = chatops_service._dispatch("hello", ["there"], source="signal")
        assert result == "Hello!"
        mock_route.assert_called_once()
        assert mock_route.call_args.kwargs["default_target"] or mock_route.call_args.args

    def test_destructive_route_returns_confirmation_and_stashes_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(
            kind="route",
            role_key="developer",
            target="acme",
            order="fix it",
            destructive=True,
        )
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            result = chatops_service._dispatch(
                "ask", ["gustave", "to", "fix", "it"], source="signal"
            )
        assert "yes" in result.lower() and "no" in result.lower()
        assert "signal" in chatops_service._pending_concierge_text
        token, pending_decision = chatops_service._pending_concierge_text["signal"]
        assert pending_decision == decision
        assert token in result

    def test_yes_with_correct_token_executes_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(
            kind="route",
            role_key="developer",
            target="acme",
            order="fix it",
            destructive=True,
        )
        chatops_service._pending_concierge_text["signal"] = ("tok123", decision)
        orch = MagicMock()
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=orch),
            patch("hivepilot.roles.get_role") as mock_get_role,
        ):
            mock_get_role.return_value = MagicMock(command_task="developer")
            result = chatops_service._dispatch("yes", ["tok123"], source="signal")
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["project_names"] == ["acme"]
        assert orch.run_task.call_args.kwargs["task_name"] == "developer"
        assert "signal" not in chatops_service._pending_concierge_text
        assert "developer" in result or "Triggered" in result

    def test_yes_with_wrong_token_does_not_execute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(kind="action", action="run", destructive=True)
        chatops_service._pending_concierge_text["signal"] = ("realtoken", decision)
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            chatops_service._dispatch("yes", ["wrongtoken"], source="signal")
        orch.run_task.assert_not_called()
        orch.run_pipeline.assert_not_called()
        assert "signal" in chatops_service._pending_concierge_text  # left untouched

    def test_no_cancels_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(kind="action", action="run", destructive=True)
        chatops_service._pending_concierge_text["signal"] = ("tok", decision)
        result = chatops_service._dispatch("no", [], source="signal")
        assert "signal" not in chatops_service._pending_concierge_text
        assert result

    def test_approve_action_execution_requires_approve_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(
            kind="action", action="approve", params={"run_id": 42}, destructive=True
        )
        chatops_service._pending_concierge_text["signal"] = ("tok", decision)
        calls: list[str] = []
        monkeypatch.setattr(chatops_service, "_verify", lambda required: calls.append(required))
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            chatops_service._dispatch("yes", ["tok"], source="signal")
        assert "approve" in calls
        orch.run_approved.assert_called_once_with(
            run_id=42, approve=True, approver="signal", reason=None
        )

    def teardown_method(self, method) -> None:
        chatops_service._pending_concierge_text.clear()
