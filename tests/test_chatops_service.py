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
