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

    def test_bare_approve_form_routes_to_approve_run(self) -> None:
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            result = chatops_service.handle_signal({"text": "approve 42"})
        orch.approve_run.assert_called_once_with(
            run_id=42, approve=True, approver="signal", reason=None
        )
        assert "42" in result

    def test_bare_deny_form_routes_to_approve_run(self) -> None:
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            chatops_service.handle_signal({"text": "deny 42"})
        orch.approve_run.assert_called_once_with(
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

    def test_status_command_shows_local_display_time_not_raw_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduces the production incident: a run stored at 09:08 UTC
        (SQLite CURRENT_TIMESTAMP format) actually started 11:08 local time
        in Europe/Paris (CEST) — the chat reply must show the LOCAL, marked
        time, not the raw UTC value."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        runs = [
            {
                "status": "failed",
                "project": "groomer",
                "task": "scan",
                "started_at": "2026-07-27 09:08:32",
            }
        ]
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=MagicMock()),
            patch("hivepilot.services.state_service.list_recent_runs", return_value=runs),
        ):
            result = chatops_service.handle_signal({"text": "/status"})
        assert "09:08" not in result
        assert "11:08" in result
        assert "CEST" in result

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

    _REQUESTER = "user-1"

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
                "ask", ["gustave", "to", "fix", "it"], source="signal", requester_id=self._REQUESTER
            )
        assert "yes" in result.lower() and "no" in result.lower()
        assert "signal" in chatops_service._pending_concierge_text
        resolved = chatops_service._pending_concierge_text.resolve("signal", self._REQUESTER)
        assert resolved is not None
        token, pending_decision = resolved
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
        chatops_service._pending_concierge_text.store(
            "signal", self._REQUESTER, ("tok123", decision)
        )
        orch = MagicMock()
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=orch),
            patch("hivepilot.roles.get_role") as mock_get_role,
        ):
            mock_get_role.return_value = MagicMock(command_task="developer")
            result = chatops_service._dispatch(
                "yes", ["tok123"], source="signal", requester_id=self._REQUESTER
            )
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["project_names"] == ["acme"]
        assert orch.run_task.call_args.kwargs["task_name"] == "developer"
        assert "signal" not in chatops_service._pending_concierge_text
        assert "developer" in result or "Triggered" in result

    def test_yes_with_wrong_token_does_not_execute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(kind="action", action="run", destructive=True)
        chatops_service._pending_concierge_text.store(
            "signal", self._REQUESTER, ("realtoken", decision)
        )
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            chatops_service._dispatch(
                "yes", ["wrongtoken"], source="signal", requester_id=self._REQUESTER
            )
        orch.run_task.assert_not_called()
        orch.run_pipeline.assert_not_called()
        assert "signal" in chatops_service._pending_concierge_text  # left untouched

    def test_no_cancels_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(kind="action", action="run", destructive=True)
        chatops_service._pending_concierge_text.store("signal", self._REQUESTER, ("tok", decision))
        result = chatops_service._dispatch("no", [], source="signal", requester_id=self._REQUESTER)
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
        chatops_service._pending_concierge_text.store("signal", self._REQUESTER, ("tok", decision))
        calls: list[str] = []
        monkeypatch.setattr(chatops_service, "_verify", lambda required: calls.append(required))
        orch = MagicMock()
        with patch.object(chatops_service, "_get_orchestrator", return_value=orch):
            chatops_service._dispatch("yes", ["tok"], source="signal", requester_id=self._REQUESTER)
        assert "approve" in calls
        orch.approve_run.assert_called_once_with(
            run_id=42, approve=True, approver="signal", reason=None
        )

    def teardown_method(self, method) -> None:
        chatops_service._pending_concierge_text.clear()


class TestPendingConciergeTextOwnerBinding:
    """Bug class #5 (`chatops_service._pending_concierge_text`): the shared
    `_dispatch` signature used to have NO per-sender identity at all, so a
    pending confirmation was keyed by SOURCE ONLY — any Slack user hitting
    `/chatops/slack` could resolve/cancel ANY other Slack user's pending
    destructive decision on that same endpoint. `PendingConfirmationStore`
    now binds every entry to a per-source `requester_id`, threaded in by
    each `handle_*` entry point."""

    _OWNER = "owner-1"
    _STRANGER = "stranger-2"

    def _decision(self):
        from hivepilot.services.concierge_service import ConciergeDecision

        return ConciergeDecision(kind="action", action="run", destructive=True)

    def _executable_decision(self):
        """A decision that, once confirmed, actually calls `orch.run_task`
        (unlike `_decision()`'s bare `action="run"` with no `params`, which
        stops early on a "Missing task name" guard) — needed to prove the
        real owner's confirmation REACHES execution, not merely that a
        stranger's doesn't."""
        from hivepilot.services.concierge_service import ConciergeDecision

        return ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix it", destructive=True
        )

    def test_different_requester_yes_does_not_execute_and_entry_survives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core F5 regression this migration closes: a DIFFERENT
        requester on the SAME source answering "yes <token>" must never
        execute someone else's pending decision, and the real owner must
        still be able to confirm it afterward."""
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        decision = self._executable_decision()
        chatops_service._pending_concierge_text.store("slack", self._OWNER, ("tok", decision))
        orch = MagicMock()
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=orch),
            patch("hivepilot.roles.get_role", return_value=MagicMock(command_task="developer")),
        ):
            chatops_service._dispatch("yes", ["tok"], source="slack", requester_id=self._STRANGER)
            orch.run_task.assert_not_called()
            assert "slack" in chatops_service._pending_concierge_text

            # The real owner can still confirm it afterward (regression guard).
            chatops_service._dispatch("yes", ["tok"], source="slack", requester_id=self._OWNER)
        orch.run_task.assert_called_once()
        assert "slack" not in chatops_service._pending_concierge_text

    def test_different_requester_no_does_not_cancel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stranger's "no" must not be able to CANCEL someone else's
        pending decision either — that's a denial-of-service against the
        real owner, not just an authorization bypass."""
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        decision = self._decision()
        chatops_service._pending_concierge_text.store("slack", self._OWNER, ("tok", decision))
        chatops_service._dispatch("no", [], source="slack", requester_id=self._STRANGER)
        assert "slack" in chatops_service._pending_concierge_text

    def test_missing_requester_id_stores_nothing_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A source that cannot supply a requester id (e.g. a caller that
        never threaded one through) must never fall back to the old
        "one shared pending decision per source" behaviour — `store()`
        records nothing at all."""
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        from hivepilot.services.concierge_service import ConciergeDecision

        decision = ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix it", destructive=True
        )
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            chatops_service._dispatch("ask", ["gustave"], source="slack", requester_id=None)
        assert "slack" not in chatops_service._pending_concierge_text

    def test_expired_pending_falls_through_to_concierge_classification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An abandoned confirmation must not remain resolvable forever —
        once expired, "yes"/"no" text falls through and is classified as
        ordinary text (never executed) instead of dangling indefinitely."""
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        decision = self._decision()
        chatops_service._pending_concierge_text.store("slack", self._OWNER, ("tok", decision))
        # White-box: force expiry (mirrors test_pending_confirmation.py's
        # own approach to testing TTL expiry deterministically).
        existing = chatops_service._pending_concierge_text._pending["slack"]
        chatops_service._pending_concierge_text._pending["slack"] = existing.__class__(
            owner_id=existing.owner_id, payload=existing.payload, expires_at=0.0
        )
        orch = MagicMock()
        from hivepilot.services.concierge_service import ConciergeDecision

        fallthrough_decision = ConciergeDecision(kind="answer", answer_text="not an answer to that")
        with (
            patch.object(chatops_service, "_get_orchestrator", return_value=orch),
            patch(
                "hivepilot.services.concierge_service.route",
                return_value=fallthrough_decision,
            ) as mock_route,
        ):
            result = chatops_service._dispatch(
                "yes", ["tok"], source="slack", requester_id=self._OWNER
            )
        orch.run_task.assert_not_called()
        orch.run_pipeline.assert_not_called()
        mock_route.assert_called_once()  # fell through to ordinary classification
        assert result == "not an answer to that"
        # Dropped as a side effect of the expired resolve().
        assert "slack" not in chatops_service._pending_concierge_text

    def teardown_method(self, method) -> None:
        chatops_service._pending_concierge_text.clear()


class TestPerSourceRequesterIdExtraction:
    """`_slack_requester_id`/`_discord_requester_id`/`_telegram_requester_id`/
    `_signal_requester_id` — must never raise on a malformed/missing payload
    (the `/chatops/*` endpoints are internet-facing webhooks), and must
    return the SAME sender identity `_pending_concierge_text` binds owner
    to."""

    def test_slack_requester_id_reads_user_id(self) -> None:
        assert chatops_service._slack_requester_id({"user_id": "U123"}) == "U123"

    def test_slack_requester_id_missing_returns_none(self) -> None:
        assert chatops_service._slack_requester_id({}) is None
        assert chatops_service._slack_requester_id({"user_id": ""}) is None

    def test_discord_requester_id_reads_author_id(self) -> None:
        assert chatops_service._discord_requester_id({"author": {"id": "D456"}}) == "D456"

    def test_discord_requester_id_missing_or_malformed_returns_none(self) -> None:
        assert chatops_service._discord_requester_id({}) is None
        assert chatops_service._discord_requester_id({"author": "not-a-dict"}) is None
        assert chatops_service._discord_requester_id({"author": {}}) is None

    def test_telegram_requester_id_reads_from_id(self) -> None:
        assert chatops_service._telegram_requester_id({"from": {"id": 789}}) == "789"

    def test_telegram_requester_id_missing_or_malformed_returns_none(self) -> None:
        assert chatops_service._telegram_requester_id({}) is None
        assert chatops_service._telegram_requester_id({"from": "not-a-dict"}) is None

    def test_signal_requester_id_reads_sender(self) -> None:
        assert chatops_service._signal_requester_id({"sender": "+15551234567"}) == "+15551234567"

    def test_signal_requester_id_missing_returns_none(self) -> None:
        assert chatops_service._signal_requester_id({"text": "hello"}) is None


class TestHandlersThreadRequesterIdIntoDispatch:
    """Each webhook entry point must extract its own platform's sender id
    and thread it into `_dispatch` — this is the wiring that makes
    `_pending_concierge_text`'s owner binding actually bind to the RIGHT
    person end-to-end, not just work in isolation at the store/resolve
    layer."""

    def test_handle_slack_threads_user_id(self) -> None:
        with patch.object(chatops_service, "_dispatch", return_value="ok") as mock_dispatch:
            chatops_service.handle_slack(
                {"command": "/hivepilot-run", "text": "acme deploy", "user_id": "U1"}
            )
        assert mock_dispatch.call_args.kwargs["requester_id"] == "U1"

    def test_handle_discord_threads_author_id(self) -> None:
        with patch.object(chatops_service, "_dispatch", return_value="ok") as mock_dispatch:
            chatops_service.handle_discord(
                {"content": "!hp run acme deploy", "author": {"id": "D1"}}
            )
        assert mock_dispatch.call_args.kwargs["requester_id"] == "D1"

    def test_handle_telegram_threads_from_id(self) -> None:
        with patch.object(chatops_service, "_dispatch", return_value="ok") as mock_dispatch:
            chatops_service.handle_telegram(
                {"message": {"text": "/hp_run acme deploy", "from": {"id": 42}}}
            )
        assert mock_dispatch.call_args.kwargs["requester_id"] == "42"

    def test_handle_signal_threads_sender(self) -> None:
        with patch.object(chatops_service, "_dispatch", return_value="ok") as mock_dispatch:
            chatops_service.handle_signal({"text": "run acme deploy", "sender": "+15551234567"})
        assert mock_dispatch.call_args.kwargs["requester_id"] == "+15551234567"

    def test_handle_signal_without_sender_passes_none(self) -> None:
        """Every pre-existing `TestHandleSignal` test in this file calls
        `handle_signal({"text": ...})` with no `sender` key — must still
        thread through as an EXPLICIT `requester_id=None`, not silently
        omitted (fail closed, never "whoever answers first")."""
        with patch.object(chatops_service, "_dispatch", return_value="ok") as mock_dispatch:
            chatops_service.handle_signal({"text": "run acme deploy"})
        assert "requester_id" in mock_dispatch.call_args.kwargs
        assert mock_dispatch.call_args.kwargs["requester_id"] is None


# ---------------------------------------------------------------------------
# `_dispatch`'s bare `approve`/`deny` command AND `_execute_concierge_decision`'s
# confirmed approve/deny action now go through the shared `Orchestrator.
# approve_run` helper instead of calling `run_approved` directly -- regression
# coverage for the same pipeline-checkpoint KeyError bug on the ChatOps
# channel (shared by Signal/Slack/Discord/Telegram's `/chatops/*` webhooks).
# ---------------------------------------------------------------------------


class _FakeApprovalOrchestrator:
    """Real `Orchestrator.approve_run` bound to fake `resume_pipeline`/
    `run_approved` -- exercises the ACTUAL routing method through
    `_dispatch`/`_execute_concierge_decision`, not a re-implementation."""

    def __init__(self) -> None:
        self.resume_pipeline_calls: list[dict] = []
        self.run_approved_calls: list[dict] = []

    def resume_pipeline(self, **kwargs):
        from hivepilot.orchestrator import RunResult

        self.resume_pipeline_calls.append(kwargs)
        return RunResult("noxys", "noxys", kwargs.get("approve", True))

    def run_approved(self, **kwargs):
        from hivepilot.orchestrator import RunResult

        self.run_approved_calls.append(kwargs)
        return RunResult("proj", "task", kwargs.get("approve", True))


from hivepilot.orchestrator import Orchestrator as _Orchestrator  # noqa: E402

_FakeApprovalOrchestrator.approve_run = _Orchestrator.approve_run  # type: ignore[attr-defined]


def _pipeline_checkpoint_approval() -> dict:
    import json

    return {
        "status": "pending",
        "task": "noxys",  # the pipeline name -- NOT a task -- is what KeyErrors
        "metadata": json.dumps({"kind": "pipeline_checkpoint", "pipeline": "noxys"}),
    }


def _per_task_approval() -> dict:
    import json

    return {"status": "pending", "task": "build", "metadata": json.dumps({})}


class TestBareApproveDenyRoutingThroughSharedHelper:
    """The bare `approve <run_id>` / `deny <run_id>` command, reached via
    `handle_signal` (Signal) and shared by every other channel's `/chatops/*`
    webhook (`handle_slack`/`handle_discord`/`handle_telegram`)."""

    def test_pipeline_checkpoint_approval_routes_to_resume_pipeline(self) -> None:
        """Live-bug regression: approving a pipeline-checkpoint run via the
        bare `approve <run_id>` command must route to `resume_pipeline`,
        never `run_approved`, and must not raise."""
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(chatops_service, "_get_orchestrator", return_value=fake_orch),
        ):
            result = chatops_service.handle_signal({"text": "approve 7"})
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.run_approved_calls == []
        assert "Approved" in result

    def test_per_task_approval_still_routes_to_run_approved(self) -> None:
        """A plain per-task approval via the bare `approve` command must
        keep routing to `run_approved` -- unchanged behavior."""
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_per_task_approval(),
            ),
            patch.object(chatops_service, "_get_orchestrator", return_value=fake_orch),
        ):
            result = chatops_service.handle_signal({"text": "approve 8"})
        assert len(fake_orch.run_approved_calls) == 1
        assert fake_orch.resume_pipeline_calls == []
        assert "Approved" in result

    def test_deny_pipeline_checkpoint_routes_to_resume_pipeline(self) -> None:
        """Denying a pipeline checkpoint via the bare `deny` command must
        also route to `resume_pipeline` (approve=False), not `run_approved`."""
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(chatops_service, "_get_orchestrator", return_value=fake_orch),
        ):
            result = chatops_service.handle_signal({"text": "deny 9 not ready"})
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.resume_pipeline_calls[0]["approve"] is False
        assert fake_orch.run_approved_calls == []
        assert "Denied" in result

    def test_unknown_run_returns_clean_message_not_crash(self) -> None:
        """A not-pending/unknown run must return the clean `ValueError`
        message, never let the exception bubble up to the `/chatops/*`
        endpoint as an unhandled 500 (same posture as api_service's 400)."""
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=None),
            patch.object(chatops_service, "_get_orchestrator", return_value=fake_orch),
        ):
            result = chatops_service.handle_signal({"text": "approve 999"})
        assert "not pending approval" in result


class TestConciergeApproveDenyRoutingThroughSharedHelper:
    """The confirmed `action: approve`/`deny` concierge decision, executed
    via `_execute_concierge_decision` after a "yes <token>" reply."""

    def _pipeline_checkpoint_decision(self, action: str):
        from hivepilot.services.concierge_service import ConciergeDecision

        return ConciergeDecision(
            kind="action", action=action, params={"run_id": 7}, destructive=True
        )

    def test_pipeline_checkpoint_approval_routes_to_resume_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        decision = self._pipeline_checkpoint_decision("approve")
        chatops_service._pending_concierge_text.store("signal", "user-1", ("tok", decision))
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(chatops_service, "_get_orchestrator", return_value=fake_orch),
        ):
            chatops_service._dispatch("yes", ["tok"], source="signal", requester_id="user-1")
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.run_approved_calls == []

    def test_per_task_approval_still_routes_to_run_approved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        decision = self._pipeline_checkpoint_decision("approve")
        chatops_service._pending_concierge_text.store("signal", "user-1", ("tok", decision))
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_per_task_approval(),
            ),
            patch.object(chatops_service, "_get_orchestrator", return_value=fake_orch),
        ):
            chatops_service._dispatch("yes", ["tok"], source="signal", requester_id="user-1")
        assert len(fake_orch.run_approved_calls) == 1
        assert fake_orch.resume_pipeline_calls == []

    def test_deny_pipeline_checkpoint_routes_to_resume_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chatops_service.settings, "chatops_concierge_enabled", True)
        decision = self._pipeline_checkpoint_decision("deny")
        chatops_service._pending_concierge_text.store("signal", "user-1", ("tok", decision))
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(chatops_service, "_get_orchestrator", return_value=fake_orch),
        ):
            chatops_service._dispatch("yes", ["tok"], source="signal", requester_id="user-1")
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.resume_pipeline_calls[0]["approve"] is False
        assert fake_orch.run_approved_calls == []

    def teardown_method(self, method) -> None:
        chatops_service._pending_concierge_text.clear()


class TestFormatApprovalsDisplaysLocalTime:
    def test_requested_at_uses_local_display_time_not_raw_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        pending = [
            {
                "run_id": 7,
                "project": "groomer",
                "task": "scan",
                "requested_at": "2026-07-27 09:08:32",
            }
        ]
        result = chatops_service._format_approvals(pending)
        assert "09:08" not in result
        assert "11:08" in result
        assert "CEST" in result


def test_no_direct_run_approved_call_in_chatops_service_source() -> None:
    """Static guard: the routing decision must live in ONE place
    (`Orchestrator.approve_run`) -- `chatops_service.py` must never call
    `run_approved`/`resume_pipeline` directly again for the approve/deny
    routing decision."""
    from pathlib import Path

    source = Path(chatops_service.__file__).read_text()
    assert ".run_approved(" not in source
    assert ".resume_pipeline(" not in source
    assert ".approve_run(" in source
