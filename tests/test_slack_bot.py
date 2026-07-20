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
import sys
import types
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.services.slack_bot as slack_bot

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
    def test_allowed_channel_calls_run_approved(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        orch.run_approved.return_value = types.SimpleNamespace(success=True)
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                app.commands["/hp-approve"],
                ack=_ack(),
                command={"channel_id": ALLOWED_CHANNEL, "text": "42"},
                respond=respond,
            )
        orch.run_approved.assert_called_once_with(run_id=42, approve=True, approver="slack")

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
        orch.run_approved.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")


# ---------------------------------------------------------------------------
# /hp-deny
# ---------------------------------------------------------------------------


class TestCmdDeny:
    def test_allowed_channel_calls_run_approved_with_deny(self) -> None:
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
        orch.run_approved.assert_called_once_with(
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
        orch.run_approved.assert_not_called()
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


# ---------------------------------------------------------------------------
# Approval-button handler (`handle_approval_action`) — the security fix.
#
# This is the regression guard: without the `_is_allowed` gate in
# `handle_approval_action`, a button press coming from a non-allowlisted
# channel would still call `run_approved` and mutate state. These tests FAIL
# on the pre-fix code.
# ---------------------------------------------------------------------------


class TestHandleApprovalAction:
    def test_allowed_channel_approve_calls_run_approved(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        orch.run_approved.return_value = types.SimpleNamespace(success=True)
        body = {"channel": {"id": ALLOWED_CHANNEL}, "user": {"username": "alice"}}
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                _approval_action_handler(app),
                ack=_ack(),
                action={"action_id": "approve_42"},
                body=body,
                respond=respond,
            )
        orch.run_approved.assert_called_once_with(
            run_id=42, approve=True, approver="slack:alice", reason=None
        )

    def test_allowed_channel_deny_calls_run_approved(self) -> None:
        app = _register()
        respond = _respond()
        orch = MagicMock()
        orch.run_approved.return_value = types.SimpleNamespace(success=True)
        body = {"channel": {"id": ALLOWED_CHANNEL}, "user": {"username": "alice"}}
        with patch.object(slack_bot, "_get_orch", return_value=orch):
            _call(
                _approval_action_handler(app),
                ack=_ack(),
                action={"action_id": "deny_42"},
                body=body,
                respond=respond,
            )
        orch.run_approved.assert_called_once_with(
            run_id=42,
            approve=False,
            approver="slack:alice",
            reason="Denied via Slack button",
        )

    def test_denied_channel_approve_button_rejected_no_state_mutation(self) -> None:
        """SECURITY REGRESSION GUARD: a button press from a non-allowlisted
        channel must NOT call run_approved and must get a rejection ack."""
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
        orch.run_approved.assert_not_called()
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
        orch.run_approved.assert_not_called()
        respond.assert_called_once_with("Unauthorized channel.")

    def test_missing_channel_in_body_treated_as_unauthorized(self) -> None:
        """Fail-closed: no channel info in the payload -> reject, don't mutate."""
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
        orch.run_approved.assert_not_called()

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
        assert action_ids == {"approve_99", "deny_99"}

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


class TestConciergeYesNo:
    def _pending_route_decision(self) -> ConciergeDecision:
        return ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix bug", destructive=True
        )

    def test_yes_correct_token_executes_via_shared_entrypoint(self) -> None:
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

    def test_yes_wrong_token_not_executed_pending_untouched(self) -> None:
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

    def test_yes_denied_channel_rejected(self) -> None:
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

    def test_no_cancels_and_pops(self) -> None:
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
