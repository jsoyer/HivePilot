"""Tests for hivepilot/services/slack_bot.py — dual-mode (socket + webhook) Slack bot.

`slack-bolt` / `slack-sdk` are NOT installed in this environment (optional `slack`
extra, see `pyproject.toml`). `_register_handlers()` itself never imports
`slack_bolt` — only `_build_app()` / `run_socket_mode()` / `handle_webhook_request()`
do so, lazily, inside their function bodies (mirroring how `plugins/infisical.py` /
`plugins/onepassword.py` guard optional SDKs). So:

  * The slash-command + button handlers are exercised by registering them against
    a lightweight `FakeBoltApp` that mimics `App.command()` / `App.action()`
    decorator registration — no real SDK needed. This mirrors
    `tests/test_telegram_bot.py`'s approach of driving handler callables directly
    with mock objects instead of a live SDK connection.
  * The three entrypoints that DO lazily `import slack_bolt` (`run_socket_mode`,
    `run_webhook_mode` -> `_build_app`, `handle_webhook_request`) get a fake
    `slack_bolt` package tree injected into `sys.modules` for the duration of the
    test (see `fake_slack_bolt` fixture) so the import succeeds without the real
    dependency and without opening any real network connection.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
import types
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.services.slack_bot as slack_bot
from hivepilot.orchestrator import Orchestrator, RunResult

ALLOWED_CHANNEL = "C-ALLOWED"
DENIED_CHANNEL = "C-DENIED"


# ---------------------------------------------------------------------------
# Fake bolt App — captures @app.command / @app.action registrations without
# needing the real slack_bolt package.
# ---------------------------------------------------------------------------


class FakeBoltApp:
    def __init__(self) -> None:
        self.commands: dict[str, Callable] = {}
        self.actions: dict[str, Callable] = {}
        self.events: dict[str, Callable] = {}

    def command(self, name: str):
        def decorator(fn: Callable) -> Callable:
            self.commands[name] = fn
            return fn

        return decorator

    def action(self, matcher: Any):
        def decorator(fn: Callable) -> Callable:
            key = matcher["action_id"] if isinstance(matcher, dict) else matcher
            self.actions[key] = fn
            return fn

        return decorator

    def event(self, event_type: str):
        def decorator(fn: Callable) -> Callable:
            self.events[event_type] = fn
            return fn

        return decorator


def _register() -> FakeBoltApp:
    app = FakeBoltApp()
    slack_bot._register_handlers(app)
    return app


def _approval_action_handler(app: FakeBoltApp) -> Callable:
    """`handle_approval_action` is registered keyed by the regex action_id
    matcher (`^(approve|deny)_\\d+$`) — not by individual action_id, since one
    handler matches both approve_<id> and deny_<id>. Other actions
    (`concierge_yes`/`concierge_no`) are registered under their own literal
    keys, so look up by the regex key specifically rather than assuming
    there's only one registered action."""
    return app.actions["^(approve|deny)_\\d+$"]


def _call(fn: Callable, **kwargs: Any) -> Any:
    """Call fn with only the kwargs it declares in its signature — mirrors
    slack_bolt's listener-argument injection (a handler only asks for the
    listener args it needs, e.g. some omit `client`)."""
    sig = inspect.signature(fn)
    accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(**accepted)


def _ack() -> MagicMock:
    return MagicMock()


def _respond() -> MagicMock:
    return MagicMock()


def _client() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ALLOWED_CHANNEL is authorized; DENIED_CHANNEL (and anything else)
    must be rejected by every handler."""
    monkeypatch.setattr(slack_bot.settings, "slack_allowed_channel_ids", [ALLOWED_CHANNEL])


@pytest.fixture(autouse=True)
def _reset_app_instance() -> Any:
    """The webhook-mode App instance is a module-level singleton — reset it
    around every test so tests don't leak state into each other."""
    slack_bot._app_instance = None
    yield
    slack_bot._app_instance = None


@pytest.fixture(autouse=True)
def _reset_pending_concierge() -> Any:
    """`_pending_concierge` is a module-level singleton — reset it around
    every test so tests don't leak pending confirmations into each other."""
    slack_bot._pending_concierge.clear()
    yield
    slack_bot._pending_concierge.clear()


@pytest.fixture(autouse=True)
def _concierge_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concierge is opt-in — default off in every test unless a test
    explicitly flips it on."""
    monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", False)


# ---------------------------------------------------------------------------
# /hp-run
# ---------------------------------------------------------------------------


class TestCmdRun:
    def test_allowed_channel_triggers_task(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        orch.run_task.return_value = []
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.commands["/hp-run"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL, "text": "acme deploy do it"},
                respond=respond,
                client=_client(),
            )
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["project_names"] == ["acme"]
        assert orch.run_task.call_args.kwargs["task_name"] == "deploy"

    def test_denied_channel_rejected_no_task_run(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.commands["/hp-run"],
                ack=_ack(),
                command={"channel_id": DENIED_CHANNEL, "text": "acme deploy"},
                respond=respond,
                client=_client(),
            )
        orch.run_task.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")


# ---------------------------------------------------------------------------
# /hp-approvals
# ---------------------------------------------------------------------------


class TestCmdApprovals:
    def test_allowed_channel_lists_pending(self) -> None:
        app = _register()
        respond = _respond()
        pending = [{"run_id": 7, "project": "acme", "task": "deploy"}]
        with patch(
            "hivepilot.services.state_service.get_pending_approvals",
            return_value=pending,
        ):
            _call(
                app.commands["/hp-approvals"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL},
                respond=respond,
                client=_client(),
            )
        # One respond() call with Block Kit blocks for the pending approval.
        assert respond.call_count == 1
        _, kwargs = respond.call_args
        assert "run #7" in kwargs["text"]

    def test_denied_channel_rejected_no_state_read(self) -> None:
        app = _register()
        respond = _respond()
        with patch("hivepilot.services.state_service.get_pending_approvals") as mock_pending:
            _call(
                app.commands["/hp-approvals"],
                ack=_ack(),
                command={"channel_id": DENIED_CHANNEL},
                respond=respond,
                client=_client(),
            )
        mock_pending.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")


# ---------------------------------------------------------------------------
# /hp-approve
# ---------------------------------------------------------------------------


class TestCmdApprove:
    def test_allowed_channel_calls_approve_run(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        orch.approve_run.return_value = types.SimpleNamespace(success=True)
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.commands["/hp-approve"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL, "text": "42"},
                respond=respond,
            )
        orch.approve_run.assert_called_once_with(run_id=42, approve=True, approver="slack")

    def test_denied_channel_rejected_no_state_mutation(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.commands["/hp-approve"],
                ack=_ack(),
                command={"channel_id": DENIED_CHANNEL, "text": "42"},
                respond=respond,
            )
        orch.approve_run.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")


# ---------------------------------------------------------------------------
# /hp-deny
# ---------------------------------------------------------------------------


class TestCmdDeny:
    def test_allowed_channel_calls_approve_run_with_deny(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.commands["/hp-deny"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL, "text": "42 not ready"},
                respond=respond,
            )
        orch.approve_run.assert_called_once_with(
            run_id=42, approve=False, approver="slack", reason="not ready"
        )

    def test_denied_channel_rejected_no_state_mutation(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.commands["/hp-deny"],
                ack=_ack(),
                command={"channel_id": DENIED_CHANNEL, "text": "42 not ready"},
                respond=respond,
            )
        orch.approve_run.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")


# ---------------------------------------------------------------------------
# /hp-status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_allowed_channel_lists_recent_runs(self) -> None:
        app = _register()
        respond = _respond()
        runs = [
            {"status": "success", "project": "acme", "task": "deploy", "started_at": "t1"},
        ]
        with patch("hivepilot.services.state_service.list_recent_runs", return_value=runs):
            _call(
                app.commands["/hp-status"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL},
                respond=respond,
            )
        out = respond.call_args.args[0]
        assert "acme" in out and "deploy" in out

    def test_denied_channel_rejected_no_state_read(self) -> None:
        app = _register()
        respond = _respond()
        with patch("hivepilot.services.state_service.list_recent_runs") as mock_runs:
            _call(
                app.commands["/hp-status"],
                ack=_ack(),
                command={"channel_id": DENIED_CHANNEL},
                respond=respond,
            )
        mock_runs.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")

    def test_started_at_uses_local_display_time_not_raw_utc(self, monkeypatch) -> None:
        """Reproduces the production incident: a run stored at 09:08 UTC
        (SQLite CURRENT_TIMESTAMP format) actually started 11:08 local time
        in Europe/Paris (CEST) — `/hp-status` must show the LOCAL, marked
        time, not the raw UTC value."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        app = _register()
        respond = _respond()
        runs = [
            {
                "status": "failed",
                "project": "groomer",
                "task": "scan",
                "started_at": "2026-07-27 09:08:32",
            }
        ]
        with patch("hivepilot.services.state_service.list_recent_runs", return_value=runs):
            _call(
                app.commands["/hp-status"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL},
                respond=respond,
            )
        out = respond.call_args.args[0]
        assert "09:08" not in out
        assert "11:08" in out
        assert "CEST" in out


# ---------------------------------------------------------------------------
# Approval-button handler (`handle_approval_action`) — the security fix.
#
# This is the regression guard: without the `_is_allowed` gate in
# `handle_approval_action`, a button press coming from a non-allowlisted
# channel would still call `approve_run` and mutate state. These tests FAIL
# on the pre-fix code.
# ---------------------------------------------------------------------------


class TestHandleApprovalAction:
    def test_allowed_channel_approve_calls_approve_run(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        orch.approve_run.return_value = types.SimpleNamespace(success=True)
        body = {"channel": {"id": ALLOWED_CHANNEL}, "user": {"username": "alice"}}
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                _approval_action_handler(app),
                ack=_ack(),
                action={"action_id": "approve_42"},
                body=body,
                respond=respond,
            )
        orch.approve_run.assert_called_once_with(
            run_id=42, approve=True, approver="slack:alice", reason=None
        )

    def test_allowed_channel_deny_calls_approve_run(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        orch.approve_run.return_value = types.SimpleNamespace(success=True)
        body = {"channel": {"id": ALLOWED_CHANNEL}, "user": {"username": "alice"}}
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                _approval_action_handler(app),
                ack=_ack(),
                action={"action_id": "deny_42"},
                body=body,
                respond=respond,
            )
        orch.approve_run.assert_called_once_with(
            run_id=42,
            approve=False,
            approver="slack:alice",
            reason="Denied via Slack button",
        )

    def test_denied_channel_approve_button_rejected_no_state_mutation(self) -> None:
        """SECURITY REGRESSION GUARD: a button press from a non-allowlisted
        channel must NOT call approve_run and must get a rejection ack."""
        app = _register()
        respond = _respond()
        orch = MagicMock()
        body = {"channel": {"id": DENIED_CHANNEL}, "user": {"username": "mallory"}}
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                _approval_action_handler(app),
                ack=_ack(),
                action={"action_id": "approve_42"},
                body=body,
                respond=respond,
            )
        orch.approve_run.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")

    def test_denied_channel_deny_button_rejected_no_state_mutation(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        body = {"channel": {"id": DENIED_CHANNEL}, "user": {"username": "mallory"}}
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                _approval_action_handler(app),
                ack=_ack(),
                action={"action_id": "deny_42"},
                body=body,
                respond=respond,
            )
        orch.approve_run.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")

    def test_missing_channel_in_body_rejected_when_allowlist_configured(self) -> None:
        """Fail-closed under a CONFIGURED (non-empty) allow-list -- the
        autouse `_allowlist` fixture sets `slack_allowed_channel_ids` to a
        non-empty list, which is WHY a missing/empty channel_id is rejected
        here (`_is_allowed` only returns True unconditionally when the list
        is EMPTY -- see F5, deliberately out of scope: an empty allow-list
        means "open to all", not "deny all"). This does NOT prove a missing
        channel is universally rejected regardless of configuration."""
        app = _register()
        respond = _respond()
        orch = MagicMock()
        body = {"user": {"username": "mallory"}}
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                _approval_action_handler(app),
                ack=_ack(),
                action={"action_id": "approve_42"},
                body=body,
                respond=respond,
            )
        orch.approve_run.assert_not_called()

    def test_invalid_action_id_still_handled_gracefully(self) -> None:
        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}, "user": {"username": "alice"}}
        _call(
            _approval_action_handler(app),
            ack=_ack(),
            action={"action_id": "approve_notanumber"},
            body=body,
            respond=respond,
        )
        respond.assert_called_once()
        assert "Invalid action" in respond.call_args.args[0]


# ---------------------------------------------------------------------------
# _approval_blocks / _format_results
# ---------------------------------------------------------------------------


class TestApprovalBlocks:
    def test_encodes_run_id_in_button_action_ids(self) -> None:
        blocks = slack_bot._approval_blocks(run_id=99, project="acme", task="deploy")
        actions_block = next(b for b in blocks if b["type"] == "actions")
        action_ids = {el["action_id"] for el in actions_block["elements"]}
        assert action_ids == {"approve_99", "deny_99", "challenge_99"}

    def test_section_mentions_project_and_task(self) -> None:
        blocks = slack_bot._approval_blocks(run_id=1, project="acme", task="deploy")
        section = blocks[0]
        assert "acme" in section["text"]["text"]
        assert "deploy" in section["text"]["text"]


class TestFormatResults:
    def test_formats_success_and_failure_rows(self) -> None:
        results = [
            types.SimpleNamespace(success=True, project="acme", target="prod", detail=None),
            types.SimpleNamespace(success=False, project="acme", target="staging", detail="boom"),
        ]
        out = slack_bot._format_results(results)
        assert "acme -> prod" in out
        assert "acme -> staging" in out
        assert "boom" in out

    def test_empty_results_returns_done(self) -> None:
        assert slack_bot._format_results([]) == "Done."


# ---------------------------------------------------------------------------
# Natural-language concierge (opt-in, settings.chatops_concierge_enabled)
# ---------------------------------------------------------------------------

from hivepilot.services.concierge_service import ConciergeDecision  # noqa: E402


def _message_event(text: str, *, channel: str = ALLOWED_CHANNEL, **extra: Any) -> dict[str, Any]:
    event = {"channel": channel, "text": text, "user": "U-ALICE"}
    event.update(extra)
    return event


# ---------------------------------------------------------------------------
# Challenge / Ask -- parity with Telegram's 🗣 Challenge / Ask button.
#
# The button press stores a pending entry keyed by channel_id (mirrors
# `_pending_concierge`'s per-channel granularity); the follow-up plain-text
# reply is captured by the SAME `event("message")` handler the concierge
# feature already uses, checked FIRST so Challenge/Ask works regardless of
# whether `chatops_concierge_enabled` is on. The actual CoS role-resolution
# + dispatch always goes through the SAME channel-agnostic
# `Orchestrator.human_challenge()` Telegram uses -- never a Slack-specific
# re-implementation.
#
# F3 security fix: the pending entry is now bound to the requesting user's
# Slack id (`owner_user_id`) and carries a TTL (`expires_at`) -- see
# `slack_bot._PendingChallenge` / `_CHALLENGE_TTL_SECONDS`. Before this fix,
# ANY later message in the channel (from any user, in any thread) was
# consumed and dispatched/logged as if the original button-presser wrote it,
# with no expiry at all.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_pending_challenges() -> Any:
    slack_bot._pending_challenges.clear()
    yield
    slack_bot._pending_challenges.clear()


def _challenge_action_handler(app: FakeBoltApp) -> Callable:
    return app.actions["^challenge_\\d+$"]


# `_message_event`'s default `"user": "U-ALICE"` (see above) is the owner id
# every helper-seeded pending entry below binds to, unless a test explicitly
# wants a mismatch/expiry scenario.
_OWNER_USER_ID = "U-ALICE"


def _pending(
    run_id: int,
    approver: str,
    *,
    owner: str = _OWNER_USER_ID,
    ttl: float = slack_bot._CHALLENGE_TTL_SECONDS,
) -> slack_bot._PendingChallenge:
    return slack_bot._PendingChallenge(run_id, approver, owner, time.time() + ttl)


class TestChallengeButtonAction:
    def test_allowed_channel_stores_pending_and_prompts(self) -> None:
        app = _register()
        respond = _respond()
        body = {
            "channel": {"id": ALLOWED_CHANNEL},
            "user": {"id": _OWNER_USER_ID, "username": "alice"},
        }
        before = time.time()
        _call(
            _challenge_action_handler(app),
            ack=_ack(),
            action={"action_id": "challenge_42"},
            body=body,
            respond=respond,
        )
        stored = slack_bot._pending_challenges[ALLOWED_CHANNEL]
        assert stored.run_id == 42
        assert stored.approver == "slack:alice"
        assert stored.owner_user_id == _OWNER_USER_ID
        assert stored.expires_at > before  # TTL was set, not left unbound
        respond.assert_called_once()
        assert "run #42" in respond.call_args.args[0]

    def test_denied_channel_rejected_no_pending_stored(self) -> None:
        """Fail-closed: a button press from a non-allowlisted channel must
        never store pending state or prompt for a follow-up."""
        app = _register()
        respond = _respond()
        body = {"channel": {"id": DENIED_CHANNEL}, "user": {"username": "mallory"}}
        _call(
            _challenge_action_handler(app),
            ack=_ack(),
            action={"action_id": "challenge_42"},
            body=body,
            respond=respond,
        )
        assert DENIED_CHANNEL not in slack_bot._pending_challenges
        respond.assert_called_once_with("Unauthorized channel.")

    def test_missing_channel_in_body_rejected_when_allowlist_configured(self) -> None:
        """Same caveat as `TestHandleApprovalAction`'s test of the same
        rename: this passes because the autouse `_allowlist` fixture sets a
        non-empty allow-list (F5's empty-list-means-open-to-all is
        deliberately unchanged, out of scope here)."""
        app = _register()
        respond = _respond()
        body = {"user": {"username": "mallory"}}
        _call(
            _challenge_action_handler(app),
            ack=_ack(),
            action={"action_id": "challenge_42"},
            body=body,
            respond=respond,
        )
        assert slack_bot._pending_challenges == {}

    def test_invalid_action_id_handled_gracefully(self) -> None:
        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}, "user": {"username": "alice"}}
        _call(
            _challenge_action_handler(app),
            ack=_ack(),
            action={"action_id": "challenge_notanumber"},
            body=body,
            respond=respond,
        )
        assert slack_bot._pending_challenges == {}
        respond.assert_called_once()
        assert "Invalid" in respond.call_args.args[0]


class TestChallengeFollowupMessage:
    def test_pending_challenge_dispatches_via_shared_human_challenge(self) -> None:
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(42, "slack:alice")
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        orch.human_challenge.return_value = "Jules says: looks fine."
        row = {"project": "acme", "task": "deploy"}
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=row),
        ):
            _call(
                app.events["message"],
                event=_message_event("why this approach?"),
                say=say,
            )
        orch.human_challenge.assert_called_once_with(42, "why this approach?", "slack:alice")
        assert ALLOWED_CHANNEL not in slack_bot._pending_challenges

        texts = [c.args[0] for c in say.call_args_list if c.args]
        assert any("why this approach?" in t for t in texts)
        assert any("Jules says: looks fine." in t for t in texts)

        # Approve/Deny/Challenge keyboard is re-sent so the operator can act again.
        blocks_calls = [c for c in say.call_args_list if c.kwargs.get("blocks")]
        assert blocks_calls
        action_ids = {
            el["action_id"]
            for block in blocks_calls[0].kwargs["blocks"]
            if block["type"] == "actions"
            for el in block["elements"]
        }
        assert action_ids == {"approve_42", "deny_42", "challenge_42"}

    def test_no_pending_challenge_falls_through_to_concierge_flag_off(self) -> None:
        """No pending challenge and concierge disabled -> pure no-op, exactly
        the pre-existing behaviour."""
        app = _register()
        say = MagicMock()
        with patch("hivepilot.services.concierge_service.route") as route:
            _call(app.events["message"], event=_message_event("hello there"), say=say)
        route.assert_not_called()
        say.assert_not_called()

    def test_denied_channel_pending_reply_rejected_no_dispatch(self) -> None:
        """Fail-closed: a reply arriving on a non-allowlisted channel must
        never dispatch to human_challenge, even if a pending entry somehow
        exists under that channel id."""
        slack_bot._pending_challenges[DENIED_CHANNEL] = _pending(7, "slack:mallory")
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.events["message"],
                event=_message_event("my answer", channel=DENIED_CHANNEL),
                say=say,
            )
        orch.human_challenge.assert_not_called()
        # Still pending -- untouched, not silently consumed.
        assert DENIED_CHANNEL in slack_bot._pending_challenges

    def test_empty_text_does_not_consume_pending_challenge(self) -> None:
        """Fail-closed: an empty/whitespace-only message must never be
        treated as an answer that resolves the pending challenge."""
        pending = _pending(42, "slack:alice")
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = pending
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.events["message"],
                event=_message_event("   "),
                say=say,
            )
        orch.human_challenge.assert_not_called()
        assert slack_bot._pending_challenges[ALLOWED_CHANNEL] == pending

    def test_bot_message_never_consumes_pending_challenge(self) -> None:
        pending = _pending(42, "slack:alice")
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = pending
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.events["message"],
                event=_message_event("my answer", bot_id="B123"),
                say=say,
            )
        orch.human_challenge.assert_not_called()
        assert ALLOWED_CHANNEL in slack_bot._pending_challenges

    def test_pending_challenge_takes_precedence_over_concierge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with the concierge flag ON, a pending Challenge/Ask reply
        must be consumed by human_challenge, never routed into the
        concierge classifier."""
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(42, "slack:alice")
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        orch.human_challenge.return_value = "ok"
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=None),
            patch("hivepilot.services.concierge_service.route") as route,
        ):
            _call(app.events["message"], event=_message_event("my answer"), say=say)
        orch.human_challenge.assert_called_once()
        route.assert_not_called()

    def test_human_challenge_error_reported_not_silently_swallowed(self) -> None:
        """F4 fix: only the exception TYPE name (escaped) reaches chat -- the
        raw message must never appear (could carry runner stderr, a token,
        or a path; see the known-unredacted RunResult.detail issue)."""
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(42, "slack:alice")
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        orch.human_challenge.side_effect = RuntimeError("boom")
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(app.events["message"], event=_message_event("my answer"), say=say)
        say.assert_called_once()
        content = say.call_args.args[0]
        assert "RuntimeError" in content
        assert "boom" not in content
        # Consumed -- an errored challenge must not stay pending forever.
        assert ALLOWED_CHANNEL not in slack_bot._pending_challenges

    def test_answer_control_sequence_neutralized(self) -> None:
        """A crafted `<!channel>` in either the operator's challenge text or
        the CoS's response must render as inert literal text, never trigger
        a broadcast ping (same guard as the concierge answer/summary text)."""
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(42, "slack:alice")
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        orch.human_challenge.return_value = "<!channel> agreed"
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=None),
        ):
            _call(
                app.events["message"],
                event=_message_event("<!channel> why?"),
                say=say,
            )
        texts = [c.args[0] for c in say.call_args_list if c.args]
        assert not any("<!channel>" in t for t in texts)
        assert any("&lt;!channel&gt;" in t for t in texts)

    def test_long_challenge_text_capped_before_dispatch(self) -> None:
        """F9 fix: Slack has no client-side cap on the follow-up reply --
        cap it server-side before it reaches the CoS / planning_context."""
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(42, "slack:alice")
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        orch.human_challenge.return_value = "ok"
        long_text = "x" * 10_000
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=None),
        ):
            _call(app.events["message"], event=_message_event(long_text), say=say)
        dispatched_text = orch.human_challenge.call_args.args[1]
        assert len(dispatched_text) == slack_bot._CHALLENGE_TEXT_MAX_LEN

    def test_long_cos_response_splits_into_multiple_messages_under_max_len(self) -> None:
        """F8: `_SLACK_TEXT_MAX_LEN` + the `split_for` call in
        `_handle_challenge_reply` had zero coverage -- a CoS response longer
        than Slack's practical per-message cap must be split into multiple
        ordered `say(...)` calls, each within the cap."""
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(42, "slack:alice")
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        long_response = "word " * 1000  # well over _SLACK_TEXT_MAX_LEN (3000)
        orch.human_challenge.return_value = long_response
        row = {"project": "acme", "task": "deploy"}
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=row),
        ):
            _call(
                app.events["message"],
                event=_message_event("short question"),
                say=say,
            )
        plain_text_calls = [c for c in say.call_args_list if c.args]
        assert len(plain_text_calls) > 1, "a long CoS response must split into >1 message"
        for c in plain_text_calls:
            assert len(c.args[0]) <= slack_bot._SLACK_TEXT_MAX_LEN


# ---------------------------------------------------------------------------
# F3 security fix: pending-challenge owner binding + TTL.
# ---------------------------------------------------------------------------


class TestChallengeOwnerBindingAndTTL:
    def test_different_user_message_does_not_consume_pending_challenge(self) -> None:
        """Alice presses Challenge; Bob's next message must NOT be consumed
        and dispatched/attributed as Alice's answer."""
        pending = _pending(42, "slack:alice", owner=_OWNER_USER_ID)
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = pending
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.events["message"],
                event=_message_event("lunch?", user="U-BOB"),
                say=say,
            )
        orch.human_challenge.assert_not_called()
        say.assert_not_called()
        # Still pending, untouched -- Alice can still answer afterwards.
        assert slack_bot._pending_challenges[ALLOWED_CHANNEL] == pending

    def test_owner_message_within_ttl_still_dispatches(self) -> None:
        """Sanity check for the fix: the legitimate button-presser's reply,
        within the TTL, must still work exactly as before."""
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(
            42, "slack:alice", owner=_OWNER_USER_ID
        )
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        orch.human_challenge.return_value = "ok"
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=None),
        ):
            _call(
                app.events["message"],
                event=_message_event("why this approach?", user=_OWNER_USER_ID),
                say=say,
            )
        orch.human_challenge.assert_called_once_with(42, "why this approach?", "slack:alice")

    def test_expired_pending_challenge_dropped_and_falls_through(self) -> None:
        """An expired entry must be dropped (not consumed) and the message
        must fall through to normal handling -- here, concierge disabled, so
        a pure no-op, never a stale dispatch to human_challenge."""
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(
            42, "slack:alice", owner=_OWNER_USER_ID, ttl=-1.0
        )
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.concierge_service.route") as route,
        ):
            _call(
                app.events["message"],
                event=_message_event("lunch?", user=_OWNER_USER_ID),
                say=say,
            )
        orch.human_challenge.assert_not_called()
        route.assert_not_called()  # concierge is off by default in this suite
        assert ALLOWED_CHANNEL not in slack_bot._pending_challenges

    def test_expired_pending_challenge_falls_through_to_concierge_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Expiry drops the entry and lets normal handling continue -- if the
        concierge is enabled, the now-unclaimed message is classified like
        any other plain message, never silently dropped or misattributed."""
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        slack_bot._pending_challenges[ALLOWED_CHANNEL] = _pending(
            42, "slack:alice", owner=_OWNER_USER_ID, ttl=-1.0
        )
        app = _register()
        say = MagicMock()
        orch = MagicMock()
        with (
            patch.object(slack_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.concierge_service.route") as route,
        ):
            _call(
                app.events["message"],
                event=_message_event("what's pending?", user=_OWNER_USER_ID),
                say=say,
            )
        orch.human_challenge.assert_not_called()
        route.assert_called_once()
        assert ALLOWED_CHANNEL not in slack_bot._pending_challenges


class TestConciergeMessageFlagOff:
    def test_flag_off_route_never_called_no_message_sent(self) -> None:
        app = _register()
        say = MagicMock()
        with patch("hivepilot.services.concierge_service.route") as route:
            _call(app.events["message"], event=_message_event("hello there"), say=say)
        route.assert_not_called()
        say.assert_not_called()


class TestConciergeMessageWhitelist:
    def test_denied_channel_route_never_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        app = _register()
        say = MagicMock()
        with patch("hivepilot.services.concierge_service.route") as route:
            _call(
                app.events["message"],
                event=_message_event("hello there", channel=DENIED_CHANNEL),
                say=say,
            )
        route.assert_not_called()
        say.assert_not_called()


class TestConciergeMessageNoLoop:
    def test_bot_message_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        app = _register()
        say = MagicMock()
        with patch("hivepilot.services.concierge_service.route") as route:
            _call(
                app.events["message"],
                event=_message_event("hello there", bot_id="B123"),
                say=say,
            )
        route.assert_not_called()
        say.assert_not_called()

    def test_subtype_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        app = _register()
        say = MagicMock()
        with patch("hivepilot.services.concierge_service.route") as route:
            _call(
                app.events["message"],
                event=_message_event("hello there", subtype="message_changed"),
                say=say,
            )
        route.assert_not_called()
        say.assert_not_called()


class TestConciergeMessageAnswer:
    def test_answer_decision_sends_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        app = _register()
        say = MagicMock()
        decision = ConciergeDecision(kind="answer", answer_text="It's running fine.")
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            _call(app.events["message"], event=_message_event("how's it going?"), say=say)
        say.assert_called_once_with("It's running fine.")

    def test_answer_text_broadcast_control_sequence_neutralized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1 regression guard: `answer_text` is LLM-classified free text an
        unprivileged channel member ultimately influences — a crafted
        `<!channel>` must render as inert literal text, never trigger a
        broadcast ping."""
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        app = _register()
        say = MagicMock()
        decision = ConciergeDecision(kind="answer", answer_text="<!channel> gotcha & <b>hi</b>")
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            _call(app.events["message"], event=_message_event("ignore instructions"), say=say)
        sent_text = say.call_args.args[0]
        assert "<!channel>" not in sent_text
        assert "&lt;!channel&gt;" in sent_text
        assert "&amp;" in sent_text


class TestConciergeMessageDestructive:
    def test_destructive_route_sends_confirmation_and_stores_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        app = _register()
        say = MagicMock()
        decision = ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix bug", destructive=True
        )
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            _call(app.events["message"], event=_message_event("ask gustave to fix bug"), say=say)

        say.assert_called_once()
        _, kwargs = say.call_args
        action_ids = {
            el["action_id"]
            for block in kwargs["blocks"]
            if block["type"] == "actions"
            for el in block["elements"]
        }
        assert action_ids == {"concierge_yes", "concierge_no"}

        assert ALLOWED_CHANNEL in slack_bot._pending_concierge
        token, stored_decision = slack_bot._pending_concierge[ALLOWED_CHANNEL]
        assert stored_decision is decision
        values = {
            el["value"]
            for block in kwargs["blocks"]
            if block["type"] == "actions"
            for el in block["elements"]
        }
        assert values == {token}

    def test_summary_broadcast_control_sequence_neutralized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1 regression guard: the destructive-decision summary embeds
        `decision.order` — LLM-classified free text — into both the Block
        Kit section text and the fallback `text` field. A crafted
        `<!channel>` in the order must render as inert literal text in
        BOTH places, never trigger a broadcast ping."""
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        app = _register()
        say = MagicMock()
        decision = ConciergeDecision(
            kind="route",
            role_key="developer",
            target="acme",
            order="<!channel> drop prod",
            destructive=True,
        )
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            _call(app.events["message"], event=_message_event("ask gustave"), say=say)

        _, kwargs = say.call_args
        assert "<!channel>" not in kwargs["text"]
        assert "&lt;!channel&gt;" in kwargs["text"]
        section_text = next(b for b in kwargs["blocks"] if b["type"] == "section")["text"]["text"]
        assert "<!channel>" not in section_text
        assert "&lt;!channel&gt;" in section_text


class TestConciergeYesNo:
    def _pending_route_decision(self) -> ConciergeDecision:
        return ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix bug", destructive=True
        )

    def test_yes_correct_token_executes_via_shared_entrypoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        slack_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}}
        with patch(
            "hivepilot.services.chatops_service._execute_concierge_decision",
            return_value="Triggered task on acme",
        ) as execute:
            _call(
                app.actions["concierge_yes"],
                ack=_ack(),
                action={"action_id": "concierge_yes", "value": "tok123"},
                body=body,
                respond=respond,
            )
        execute.assert_called_once()
        args = execute.call_args.args
        assert args[1] is decision
        assert args[2] == f"slack:{ALLOWED_CHANNEL}"
        respond.assert_called_once_with("Triggered task on acme")
        assert ALLOWED_CHANNEL not in slack_bot._pending_concierge

    def test_yes_wrong_token_not_executed_pending_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        slack_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}}
        with patch("hivepilot.services.chatops_service._execute_concierge_decision") as execute:
            _call(
                app.actions["concierge_yes"],
                ack=_ack(),
                action={"action_id": "concierge_yes", "value": "stale-token"},
                body=body,
                respond=respond,
            )
        execute.assert_not_called()
        assert "expired" in respond.call_args.args[0].lower()
        # Pending entry must remain untouched — the real confirmation can
        # still be answered correctly afterwards.
        assert slack_bot._pending_concierge[ALLOWED_CHANNEL] == ("tok123", decision)

    def test_overwrite_scenario_stale_button_never_executes_new_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """decision A is pending with token A; before the user presses A's
        Yes button, decision B overwrites the pending entry for the same
        channel (different token, different content). Pressing A's stale
        button must execute NOTHING — never A, and never B."""
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        decision_a = self._pending_route_decision()
        decision_b = ConciergeDecision(
            kind="action",
            action="run_pipeline",
            target="acme-api",
            params={"pipeline": "company"},
            destructive=True,
        )
        slack_bot._pending_concierge[ALLOWED_CHANNEL] = ("token_a", decision_a)
        # A newer destructive message overwrites the pending entry before the
        # user acts on A's button.
        slack_bot._pending_concierge[ALLOWED_CHANNEL] = ("token_b", decision_b)

        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}}
        with patch("hivepilot.services.chatops_service._execute_concierge_decision") as execute:
            _call(
                app.actions["concierge_yes"],
                ack=_ack(),
                action={"action_id": "concierge_yes", "value": "token_a"},  # A's stale button
                body=body,
                respond=respond,
            )
        execute.assert_not_called()
        assert "expired" in respond.call_args.args[0].lower()
        # B is still pending, untouched, and can still be confirmed correctly later.
        assert slack_bot._pending_concierge[ALLOWED_CHANNEL] == ("token_b", decision_b)

    def test_yes_denied_channel_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        slack_bot._pending_concierge[DENIED_CHANNEL] = ("tok123", decision)
        app = _register()
        respond = _respond()
        body = {"channel": {"id": DENIED_CHANNEL}}
        with patch("hivepilot.services.chatops_service._execute_concierge_decision") as execute:
            _call(
                app.actions["concierge_yes"],
                ack=_ack(),
                action={"action_id": "concierge_yes", "value": "tok123"},
                body=body,
                respond=respond,
            )
        execute.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")
        assert DENIED_CHANNEL in slack_bot._pending_concierge

    def test_no_cancels_and_pops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        slack_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}}
        _call(
            app.actions["concierge_no"],
            ack=_ack(),
            action={"action_id": "concierge_no", "value": "tok123"},
            body=body,
            respond=respond,
        )
        respond.assert_called_once_with("Cancelled.")
        assert ALLOWED_CHANNEL not in slack_bot._pending_concierge


class TestConciergeYesNoFlagOff:
    """F2 regression guard: a runtime flag toggle-off must not leave an
    already-rendered Yes/No button executable. `_concierge_off_by_default`
    keeps the flag off for these tests (no explicit monkeypatch needed)."""

    def _pending_route_decision(self) -> ConciergeDecision:
        return ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix bug", destructive=True
        )

    def test_yes_does_not_execute_when_flag_off(self) -> None:
        decision = self._pending_route_decision()
        slack_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}}
        with patch("hivepilot.services.chatops_service._execute_concierge_decision") as execute:
            _call(
                app.actions["concierge_yes"],
                ack=_ack(),
                action={"action_id": "concierge_yes", "value": "tok123"},
                body=body,
                respond=respond,
            )
        execute.assert_not_called()
        respond.assert_not_called()
        # Pending entry untouched — flag off is a no-op, not a silent cancel.
        assert slack_bot._pending_concierge[ALLOWED_CHANNEL] == ("tok123", decision)

    def test_no_does_not_pop_when_flag_off(self) -> None:
        decision = self._pending_route_decision()
        slack_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        app = _register()
        respond = _respond()
        body = {"channel": {"id": ALLOWED_CHANNEL}}
        _call(
            app.actions["concierge_no"],
            ack=_ack(),
            action={"action_id": "concierge_no", "value": "tok123"},
            body=body,
            respond=respond,
        )
        respond.assert_not_called()
        assert slack_bot._pending_concierge[ALLOWED_CHANNEL] == ("tok123", decision)


# ---------------------------------------------------------------------------
# Optional-SDK smoke tests — run_socket_mode / run_webhook_mode /
# handle_webhook_request wire up without a real Slack connection.
# ---------------------------------------------------------------------------


class _FakeApp:
    """Stand-in for slack_bolt.App — records init kwargs, supports the same
    decorator surface `_register_handlers` uses."""

    instances: list["_FakeApp"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        _FakeApp.instances.append(self)

    def command(self, name: str):
        def decorator(fn: Callable) -> Callable:
            return fn

        return decorator

    def action(self, matcher: Any):
        def decorator(fn: Callable) -> Callable:
            return fn

        return decorator

    def event(self, event_type: str):
        def decorator(fn: Callable) -> Callable:
            return fn

        return decorator


class _FakeSocketModeHandler:
    instances: list["_FakeSocketModeHandler"] = []

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token
        self.started = False
        _FakeSocketModeHandler.instances.append(self)

    def start(self) -> None:
        self.started = True


class _FakeSlackRequestHandler:
    instances: list["_FakeSlackRequestHandler"] = []

    def __init__(self, app: Any) -> None:
        self.app = app
        _FakeSlackRequestHandler.instances.append(self)

    async def handle(self, request: Any) -> str:
        return "handled"


@pytest.fixture()
def fake_slack_bolt(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake slack_bolt package tree into sys.modules so the lazy
    `from slack_bolt import App` / adapter imports succeed without the real
    (uninstalled) dependency and without any real network connection."""
    _FakeApp.instances.clear()
    _FakeSocketModeHandler.instances.clear()
    _FakeSlackRequestHandler.instances.clear()

    fake_bolt = types.ModuleType("slack_bolt")
    fake_bolt.App = _FakeApp  # type: ignore[attr-defined]

    fake_adapter = types.ModuleType("slack_bolt.adapter")

    fake_socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    fake_socket_mode.SocketModeHandler = _FakeSocketModeHandler  # type: ignore[attr-defined]

    fake_fastapi = types.ModuleType("slack_bolt.adapter.fastapi")
    fake_fastapi.SlackRequestHandler = _FakeSlackRequestHandler  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "slack_bolt", fake_bolt)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", fake_adapter)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode", fake_socket_mode)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.fastapi", fake_fastapi)

    monkeypatch.setattr(slack_bot.settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(slack_bot.settings, "slack_signing_secret", "sign-test")
    monkeypatch.setattr(slack_bot.settings, "slack_app_token", "xapp-test")
    return types.SimpleNamespace(
        App=_FakeApp,
        SocketModeHandler=_FakeSocketModeHandler,
        SlackRequestHandler=_FakeSlackRequestHandler,
    )


class TestRunSocketMode:
    def test_builds_app_and_starts_handler(self, fake_slack_bolt: Any) -> None:
        slack_bot.run_socket_mode()
        assert len(_FakeApp.instances) == 1
        assert len(_FakeSocketModeHandler.instances) == 1
        handler = _FakeSocketModeHandler.instances[0]
        assert handler.started is True
        assert handler.token == "xapp-test"


class TestRunWebhookMode:
    def test_returns_lazily_built_singleton_app(self, fake_slack_bolt: Any) -> None:
        app1 = slack_bot.run_webhook_mode()
        app2 = slack_bot.run_webhook_mode()
        assert app1 is app2
        assert isinstance(app1, _FakeApp)
        assert len(_FakeApp.instances) == 1


class TestHandleWebhookRequest:
    def test_delegates_to_slack_request_handler(self, fake_slack_bolt: Any) -> None:
        request = MagicMock()
        result = asyncio.run(slack_bot.handle_webhook_request(request))
        assert result == "handled"
        assert len(_FakeSlackRequestHandler.instances) == 1


class TestShutdown:
    def test_shutdown_releases_singleton(self, fake_slack_bolt: Any) -> None:
        slack_bot.run_webhook_mode()
        assert slack_bot._app_instance is not None
        slack_bot.shutdown()
        assert slack_bot._app_instance is None


# ---------------------------------------------------------------------------
# `/hp-approve` / `/hp-deny` now go through the shared `Orchestrator.approve_run`
# helper (also used by `api_service.handle_approval` / `telegram_bot`) instead
# of calling `run_approved` directly -- regression coverage for the same
# pipeline-checkpoint KeyError bug on the Slack channel.
# ---------------------------------------------------------------------------


class _FakeApprovalOrchestrator:
    """Real `Orchestrator.approve_run` bound to fake `resume_pipeline`/
    `run_approved` -- exercises the ACTUAL routing method through the Slack
    handler, not a re-implementation of it."""

    def __init__(self) -> None:
        self.resume_pipeline_calls: list[dict] = []
        self.run_approved_calls: list[dict] = []

    def resume_pipeline(self, **kwargs):
        self.resume_pipeline_calls.append(kwargs)
        return RunResult("noxys", "noxys", kwargs.get("approve", True))

    def run_approved(self, **kwargs):
        self.run_approved_calls.append(kwargs)
        return RunResult("proj", "task", kwargs.get("approve", True))


_FakeApprovalOrchestrator.approve_run = Orchestrator.approve_run  # type: ignore[attr-defined]


def _pipeline_checkpoint_approval() -> dict:
    return {
        "status": "pending",
        "task": "noxys",  # the pipeline name -- NOT a task -- is what KeyErrors
        "metadata": json.dumps({"kind": "pipeline_checkpoint", "pipeline": "noxys"}),
    }


def _per_task_approval() -> dict:
    return {"status": "pending", "task": "build", "metadata": json.dumps({})}


class TestSlackApprovalRoutingThroughSharedHelper:
    def test_pipeline_checkpoint_approval_routes_to_resume_pipeline(self) -> None:
        """Live-bug regression on the Slack channel: approving a
        pipeline-checkpoint run via `/hp-approve` must route to
        `resume_pipeline`, never `run_approved`, and must not raise."""
        app = _register()
        respond = _respond()
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(slack_bot, "_get_orch", return_value=fake_orch),
        ):
            _call(
                app.commands["/hp-approve"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL, "text": "7"},
                respond=respond,
            )
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.run_approved_calls == []

    def test_per_task_approval_still_routes_to_run_approved(self) -> None:
        """A plain per-task approval via `/hp-approve` must keep routing to
        `run_approved` -- unchanged behavior."""
        app = _register()
        respond = _respond()
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_per_task_approval(),
            ),
            patch.object(slack_bot, "_get_orch", return_value=fake_orch),
        ):
            _call(
                app.commands["/hp-approve"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL, "text": "8"},
                respond=respond,
            )
        assert len(fake_orch.run_approved_calls) == 1
        assert fake_orch.resume_pipeline_calls == []

    def test_deny_pipeline_checkpoint_routes_to_resume_pipeline(self) -> None:
        """Denying a pipeline checkpoint via `/hp-deny` must also route to
        `resume_pipeline` (approve=False), not `run_approved`."""
        app = _register()
        respond = _respond()
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(slack_bot, "_get_orch", return_value=fake_orch),
        ):
            _call(
                app.commands["/hp-deny"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL, "text": "9 not ready"},
                respond=respond,
            )
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.resume_pipeline_calls[0]["approve"] is False
        assert fake_orch.run_approved_calls == []

    def test_no_direct_run_approved_call_in_slack_bot_source(self) -> None:
        """Static guard: the routing decision must live in ONE place
        (`Orchestrator.approve_run`) -- `slack_bot.py` must never call
        `run_approved`/`resume_pipeline` directly again for the
        approve/deny routing decision."""
        from pathlib import Path

        source = Path(slack_bot.__file__).read_text()
        assert ".run_approved(" not in source
        assert ".resume_pipeline(" not in source
        assert ".approve_run(" in source
