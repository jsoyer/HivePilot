"""Tests for hivepilot/services/discord_bot.py — HTTP-interactions + gateway Discord bot.

`discord.py` / `PyNaCl` are NOT installed in this environment (optional `discord`
extra, see `pyproject.toml`). Mirrors `tests/test_slack_bot.py` / `test_telegram_bot.py`:

  * The HTTP-interactions code path (`handle_interaction`, `_handle_component`,
    `_exec_*`) never imports `discord` — it's driven directly with plain dicts/JSON,
    no SDK needed. Background dispatch normally happens on a `threading.Thread`;
    tests replace `discord_bot.threading` with a synchronous stand-in so a call to
    `handle_interaction`/`_handle_component` produces its side effects immediately.
  * `run_gateway()` (the only entrypoint that lazily `import discord`) gets a fake
    `discord` package tree injected into `sys.modules` for the duration of the test
    (see `fake_discord` fixture) so the import succeeds without the real dependency
    and without opening any network connection.

Security note: unlike the (pre-fix) Slack bot, Discord's `handle_interaction` runs
the `_is_allowed(guild_id, channel_id)` gate ONCE, before branching on interaction
type — so both APPLICATION_COMMAND (slash commands) *and* MESSAGE_COMPONENT (the
Approve/Deny buttons) are covered by the same fail-closed check. The
`TestMessageComponentApprovalButton` class below is the regression guard for that:
it fails if the shared gate is ever removed or the button branch is special-cased
to skip it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hivepilot.services.discord_bot as discord_bot
from hivepilot.orchestrator import Orchestrator, RunResult
from hivepilot.services.concierge_service import ConciergeDecision

ALLOWED_GUILD = 111
ALLOWED_CHANNEL = 222
DENIED_GUILD = 999
DENIED_CHANNEL = 888


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ALLOWED_GUILD/ALLOWED_CHANNEL are authorized; anything else must be
    rejected by every handler (guild AND channel are both enforced)."""
    monkeypatch.setattr(discord_bot.settings, "discord_allowed_guild_ids", [ALLOWED_GUILD])
    monkeypatch.setattr(discord_bot.settings, "discord_allowed_channel_ids", [ALLOWED_CHANNEL])


@pytest.fixture(autouse=True)
def _reset_pending_concierge() -> Any:
    """`_pending_concierge` is a module-level singleton — reset it around
    every test so tests don't leak pending confirmations into each other."""
    discord_bot._pending_concierge.clear()
    yield
    discord_bot._pending_concierge.clear()


@pytest.fixture(autouse=True)
def _concierge_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concierge is opt-in — default off in every test unless a test
    explicitly flips it on."""
    monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", False)


class _ImmediateThread:
    """Stand-in for `threading.Thread` that runs `target` synchronously on `.start()`.

    `handle_interaction` / `_handle_component` dispatch real work on a background
    thread so the HTTP handler can return a DEFERRED response immediately. Tests
    need the side effects to happen before assertions run, so this collapses the
    background thread into the calling thread.
    """

    def __init__(
        self,
        target: Callable | None = None,
        args: tuple = (),
        kwargs: dict | None = None,
        daemon: bool | None = None,
    ) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _sync_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_threading = types.SimpleNamespace(Thread=_ImmediateThread)
    monkeypatch.setattr(discord_bot, "threading", fake_threading)


# ---------------------------------------------------------------------------
# Interaction payload builders
# ---------------------------------------------------------------------------


def _command_body(
    name: str,
    options: dict[str, Any] | None = None,
    *,
    guild_id: int | None = ALLOWED_GUILD,
    channel_id: int | None = ALLOWED_CHANNEL,
) -> bytes:
    payload: dict[str, Any] = {
        "type": 2,
        "application_id": "app-1",
        "token": "tok-1",
        "data": {
            "name": name,
            "options": [{"name": k, "value": v} for k, v in (options or {}).items()],
        },
    }
    if guild_id is not None:
        payload["guild_id"] = guild_id
    if channel_id is not None:
        payload["channel_id"] = channel_id
    return json.dumps(payload).encode()


def _component_body(
    custom_id: str,
    *,
    guild_id: int | None = ALLOWED_GUILD,
    channel_id: int | None = ALLOWED_CHANNEL,
    username: str = "alice",
) -> bytes:
    payload: dict[str, Any] = {
        "type": 3,
        "application_id": "app-1",
        "token": "tok-1",
        "data": {"custom_id": custom_id},
        "member": {"user": {"username": username, "id": 42}},
    }
    if guild_id is not None:
        payload["guild_id"] = guild_id
    if channel_id is not None:
        payload["channel_id"] = channel_id
    return json.dumps(payload).encode()


def _modal_submit_body(
    custom_id: str,
    *,
    text_value: Any = "why this approach?",
    text_custom_id: str = "challenge_text",
    guild_id: int | None = ALLOWED_GUILD,
    channel_id: int | None = ALLOWED_CHANNEL,
    username: str = "alice",
    omit_components: bool = False,
) -> bytes:
    """Build a MODAL_SUBMIT (interaction type 5) interaction payload — the
    shape Discord sends when the Challenge/Ask modal's text field is
    submitted (nested one level inside an action-row `components` list,
    same as the real API)."""
    data: dict[str, Any] = {"custom_id": custom_id}
    if not omit_components:
        components: list[dict[str, Any]] = []
        if text_value is not _OMIT:
            components.append(
                {
                    "type": 1,
                    "components": [{"type": 4, "custom_id": text_custom_id, "value": text_value}],
                }
            )
        data["components"] = components
    payload: dict[str, Any] = {
        "type": 5,
        "application_id": "app-1",
        "token": "tok-1",
        "data": data,
        "member": {"user": {"username": username, "id": 42}},
    }
    if guild_id is not None:
        payload["guild_id"] = guild_id
    if channel_id is not None:
        payload["channel_id"] = channel_id
    return json.dumps(payload).encode()


_OMIT = object()


# ---------------------------------------------------------------------------
# _is_allowed
# ---------------------------------------------------------------------------


class TestIsAllowed:
    def test_allowed_guild_and_channel(self) -> None:
        assert discord_bot._is_allowed(ALLOWED_GUILD, ALLOWED_CHANNEL) is True

    def test_denied_guild_rejected(self) -> None:
        assert discord_bot._is_allowed(DENIED_GUILD, ALLOWED_CHANNEL) is False

    def test_denied_channel_rejected(self) -> None:
        assert discord_bot._is_allowed(ALLOWED_GUILD, DENIED_CHANNEL) is False

    def test_none_guild_and_channel_rejected_when_lists_configured(self) -> None:
        assert discord_bot._is_allowed(None, None) is False

    def test_open_when_both_lists_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(discord_bot.settings, "discord_allowed_guild_ids", [])
        monkeypatch.setattr(discord_bot.settings, "discord_allowed_channel_ids", [])
        assert discord_bot._is_allowed(None, None) is True
        assert discord_bot._is_allowed(DENIED_GUILD, DENIED_CHANNEL) is True


# ---------------------------------------------------------------------------
# handle_interaction — PING
# ---------------------------------------------------------------------------


class TestHandleInteractionPing:
    def test_ping_returns_pong_without_allowlist_check(self) -> None:
        body = json.dumps({"type": 1}).encode()
        result = discord_bot.handle_interaction(body, "sig", "ts")
        assert result == {"type": 1}


# ---------------------------------------------------------------------------
# APPLICATION_COMMAND — /run
# ---------------------------------------------------------------------------


class TestCmdRun:
    def test_allowed_triggers_task(self) -> None:
        orch = MagicMock()
        orch.run_task.return_value = []
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _command_body("run", {"project": "acme", "task": "deploy"}), "sig", "ts"
            )
        assert result == {"type": 5}
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["project_names"] == ["acme"]
        assert orch.run_task.call_args.kwargs["task_name"] == "deploy"
        followup.assert_called_once()

    def test_denied_rejected_no_task_run(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _command_body(
                    "run",
                    {"project": "acme", "task": "deploy"},
                    guild_id=DENIED_GUILD,
                    channel_id=DENIED_CHANNEL,
                ),
                "sig",
                "ts",
            )
        assert result["type"] == 4
        assert result["data"]["content"] == "Unauthorized."
        orch.run_task.assert_not_called()
        followup.assert_not_called()


# ---------------------------------------------------------------------------
# APPLICATION_COMMAND — /approvals
# ---------------------------------------------------------------------------


class TestCmdApprovals:
    def test_allowed_lists_pending(self) -> None:
        pending = [{"run_id": 7, "project": "acme", "task": "deploy"}]
        with (
            patch(
                "hivepilot.services.state_service.get_pending_approvals",
                return_value=pending,
            ),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_command_body("approvals"), "sig", "ts")
        followup.assert_called_once()
        _, kwargs = followup.call_args
        assert "#7" in followup.call_args.args[2]["content"]

    def test_denied_rejected_no_state_read(self) -> None:
        with (
            patch("hivepilot.services.state_service.get_pending_approvals") as mock_pending,
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(
                _command_body("approvals", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL),
                "sig",
                "ts",
            )
        mock_pending.assert_not_called()
        followup.assert_not_called()


# ---------------------------------------------------------------------------
# APPLICATION_COMMAND — /approve, /deny
# ---------------------------------------------------------------------------


class TestCmdApprove:
    def test_allowed_calls_approve_run(self) -> None:
        orch = MagicMock()
        orch.approve_run.return_value = types.SimpleNamespace(success=True)
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_command_body("approve", {"run_id": 42}), "sig", "ts")
        orch.approve_run.assert_called_once_with(run_id=42, approve=True, approver="discord")
        followup.assert_called_once()

    def test_denied_rejected_no_state_mutation(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _command_body(
                    "approve", {"run_id": 42}, guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL
                ),
                "sig",
                "ts",
            )
        assert result["data"]["content"] == "Unauthorized."
        orch.approve_run.assert_not_called()
        followup.assert_not_called()


class TestCmdDeny:
    def test_allowed_calls_approve_run_with_deny(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(
                _command_body("deny", {"run_id": 42, "reason": "not ready"}), "sig", "ts"
            )
        orch.approve_run.assert_called_once_with(
            run_id=42, approve=False, approver="discord", reason="not ready"
        )
        followup.assert_called_once()

    def test_denied_rejected_no_state_mutation(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(
                _command_body(
                    "deny",
                    {"run_id": 42, "reason": "not ready"},
                    guild_id=DENIED_GUILD,
                    channel_id=DENIED_CHANNEL,
                ),
                "sig",
                "ts",
            )
        orch.approve_run.assert_not_called()
        followup.assert_not_called()


# ---------------------------------------------------------------------------
# APPLICATION_COMMAND — /status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_allowed_lists_recent_runs(self) -> None:
        runs = [{"status": "success", "project": "acme", "task": "deploy", "started_at": "t1"}]
        with (
            patch("hivepilot.services.state_service.list_recent_runs", return_value=runs),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_command_body("status"), "sig", "ts")
        content = followup.call_args.args[2]["content"]
        assert "acme" in content and "deploy" in content

    def test_denied_rejected_no_state_read(self) -> None:
        with (
            patch("hivepilot.services.state_service.list_recent_runs") as mock_runs,
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(
                _command_body("status", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL),
                "sig",
                "ts",
            )
        mock_runs.assert_not_called()
        followup.assert_not_called()

    def test_started_at_uses_local_display_time_not_raw_utc(self, monkeypatch) -> None:
        """Reproduces the production incident: a run stored at 09:08 UTC
        (SQLite CURRENT_TIMESTAMP format) actually started 11:08 local time
        in Europe/Paris (CEST) — `/status` must show the LOCAL, marked
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
            patch("hivepilot.services.state_service.list_recent_runs", return_value=runs),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_command_body("status"), "sig", "ts")
        content = followup.call_args.args[2]["content"]
        assert "09:08" not in content
        assert "11:08" in content
        assert "CEST" in content


# ---------------------------------------------------------------------------
# MESSAGE_COMPONENT (button) — approve/deny — the security-sensitive path.
#
# `handle_interaction` runs the `_is_allowed` gate BEFORE branching on
# interaction type, so this is the same gate exercised by the command tests
# above. These tests are the regression guard: if the gate were ever moved
# to only cover the APPLICATION_COMMAND branch (mirroring the pre-fix Slack
# bug, where the button handler had no gate at all), these would fail.
# ---------------------------------------------------------------------------


class TestMessageComponentApprovalButton:
    def test_allowed_approve_calls_approve_run(self) -> None:
        orch = MagicMock()
        orch.approve_run.return_value = types.SimpleNamespace(success=True)
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(_component_body("approve:42"), "sig", "ts")
        assert result == {"type": 5}
        orch.approve_run.assert_called_once_with(run_id=42, approve=True, approver="discord")
        followup.assert_called_once()

    def test_allowed_deny_calls_approve_run(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_component_body("deny:42"), "sig", "ts")
        orch.approve_run.assert_called_once()
        kwargs = orch.approve_run.call_args.kwargs
        assert kwargs["run_id"] == 42
        assert kwargs["approve"] is False
        assert "alice" in kwargs["reason"]
        followup.assert_called_once()

    def test_denied_channel_approve_button_rejected_no_state_mutation(self) -> None:
        """SECURITY REGRESSION GUARD: a button press from a non-allowlisted
        guild/channel must NOT call approve_run and must get a rejection,
        with the background dispatch never started."""
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _component_body("approve:42", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL),
                "sig",
                "ts",
            )
        assert result["type"] == 4
        assert result["data"]["content"] == "Unauthorized."
        orch.approve_run.assert_not_called()
        followup.assert_not_called()

    def test_denied_channel_deny_button_rejected_no_state_mutation(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(
                _component_body("deny:42", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL),
                "sig",
                "ts",
            )
        orch.approve_run.assert_not_called()
        followup.assert_not_called()

    def test_missing_guild_and_channel_treated_as_unauthorized(self) -> None:
        """Fail-closed: no guild_id/channel_id in the payload -> reject, don't mutate."""
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _component_body("approve:42", guild_id=None, channel_id=None), "sig", "ts"
            )
        assert result["data"]["content"] == "Unauthorized."
        orch.approve_run.assert_not_called()
        followup.assert_not_called()

    def test_invalid_custom_id_handled_gracefully(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_component_body("approve:notanumber"), "sig", "ts")
        orch.approve_run.assert_not_called()
        followup.assert_called_once()
        assert "Invalid component id" in followup.call_args.args[2]["content"]


# ---------------------------------------------------------------------------
# Challenge / Ask — parity with Telegram's Challenge / Ask button.
#
# Discord has no privileged plain-text listener in HTTP-interactions mode
# (only gateway mode can receive `on_message`, and only when the privileged
# Message Content intent + `chatops_concierge_enabled` are both on) — so the
# follow-up text is captured via a MODAL (component type 9) opened
# synchronously from the button press, instead of a plain-text reply.
# Submitting the modal (interaction type 5 = MODAL_SUBMIT) dispatches
# through the SAME channel-agnostic `Orchestrator.human_challenge()`
# Telegram/Slack use — never a Discord-specific re-implementation.
# ---------------------------------------------------------------------------


class TestChallengeButtonOpensModal:
    def test_allowed_returns_modal_with_run_id_encoded(self) -> None:
        result = discord_bot.handle_interaction(_component_body("challenge:42"), "sig", "ts")
        assert result["type"] == 9
        assert result["data"]["custom_id"] == "challenge_modal:42"
        text_input = result["data"]["components"][0]["components"][0]
        assert text_input["custom_id"] == "challenge_text"
        assert text_input["required"] is True

    def test_denied_channel_rejected_no_modal(self) -> None:
        """Fail-closed: a button press from a non-allowlisted guild/channel
        must never open the modal."""
        result = discord_bot.handle_interaction(
            _component_body("challenge:42", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL),
            "sig",
            "ts",
        )
        assert result["type"] == 4
        assert result["data"]["content"] == "Unauthorized."

    def test_invalid_run_id_rejected_no_modal(self) -> None:
        result = discord_bot.handle_interaction(
            _component_body("challenge:notanumber"), "sig", "ts"
        )
        assert result["type"] == 4
        assert "Invalid component id" in result["data"]["content"]

    def test_approve_deny_still_dispatch_to_thread_not_modal(self) -> None:
        """Regression guard: only the challenge button short-circuits to a
        synchronous modal response — approve/deny keep their existing
        threaded, deferred dispatch."""
        orch = MagicMock()
        orch.approve_run.return_value = types.SimpleNamespace(success=True)
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message"),
        ):
            result = discord_bot.handle_interaction(_component_body("approve:42"), "sig", "ts")
        assert result == {"type": 5}
        orch.approve_run.assert_called_once()


class TestChallengeModalSubmit:
    def test_allowed_dispatches_via_shared_human_challenge(self) -> None:
        orch = MagicMock()
        orch.human_challenge.return_value = "Jules says: looks fine."
        row = {"project": "acme", "task": "deploy"}
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=row),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:42", text_value="why this approach?"),
                "sig",
                "ts",
            )
        assert result == {"type": 5}
        orch.human_challenge.assert_called_once_with(42, "why this approach?", "discord:alice")
        assert followup.called
        sent_texts = [c.args[2]["content"] for c in followup.call_args_list]
        assert any("why this approach?" in t for t in sent_texts)
        assert any("Jules says: looks fine." in t for t in sent_texts)
        # Every followup must carry the anti-mass-ping guard.
        for c in followup.call_args_list:
            assert c.args[2]["allowed_mentions"] == {"parse": []}
        # Approve/Deny/Challenge buttons are re-attached so the operator can
        # act again, in the SAME channel (via the followup webhook).
        last_payload = followup.call_args_list[-1].args[2]
        custom_ids = {
            el["custom_id"] for row_ in last_payload["components"] for el in row_["components"]
        }
        assert custom_ids == {"approve:42", "deny:42", "challenge:42"}

    def test_denied_channel_rejected_no_dispatch(self) -> None:
        """Fail-closed: a modal submission from a non-allowlisted
        guild/channel must never dispatch to human_challenge."""
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _modal_submit_body(
                    "challenge_modal:42", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL
                ),
                "sig",
                "ts",
            )
        assert result["type"] == 4
        assert result["data"]["content"] == "Unauthorized."
        orch.human_challenge.assert_not_called()
        followup.assert_not_called()

    def test_unknown_modal_custom_id_rejected(self) -> None:
        """Fail-closed: an unrecognized modal (not our Challenge/Ask modal)
        must be rejected, never treated as an answer."""
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            result = discord_bot.handle_interaction(
                _modal_submit_body("some_other_modal:42"), "sig", "ts"
            )
        assert result["type"] == 4
        assert "Unknown modal" in result["data"]["content"]
        orch.human_challenge.assert_not_called()

    def test_unknown_modal_custom_id_with_parseable_int_suffix_rejected(self) -> None:
        """Test-hardening: `test_unknown_modal_custom_id_rejected` above uses
        a custom_id ("some_other_modal:42") that only accidentally fails the
        prefix check by being a different length/shape from
        `challenge_modal:<id>`. This case's suffix is ALSO a cleanly
        parseable int, so it would slip through a broken guard that (e.g.)
        matched on "contains a colon followed by digits" instead of the
        actual `str.startswith(_CHALLENGE_MODAL_PREFIX)` check -- proving the
        rejection is really keyed off the prefix, not the shape of the id."""
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            result = discord_bot.handle_interaction(
                _modal_submit_body("evil_modal_prefix:42"), "sig", "ts"
            )
        assert result["type"] == 4
        assert "Unknown modal" in result["data"]["content"]
        orch.human_challenge.assert_not_called()

    def test_invalid_run_id_in_modal_custom_id_rejected(self) -> None:
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            result = discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:notanumber"), "sig", "ts"
            )
        assert result["type"] == 4
        assert "Invalid modal id" in result["data"]["content"]
        orch.human_challenge.assert_not_called()

    def test_empty_text_rejected_not_dispatched(self) -> None:
        """SECURITY REGRESSION GUARD (empty-value fail-open bug class): an
        empty/whitespace-only modal submission must be REJECTED, never
        silently treated as a valid answer that dispatches to the Chief of
        Staff."""
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            result = discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:42", text_value="   "), "sig", "ts"
            )
        assert result["type"] == 4
        assert result["data"]["flags"] == 64
        assert "empty" in result["data"]["content"].lower()
        orch.human_challenge.assert_not_called()

    def test_missing_text_component_rejected_not_dispatched(self) -> None:
        """Malformed payload: no matching text-input component at all."""
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            result = discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:42", text_value=_OMIT), "sig", "ts"
            )
        assert result["type"] == 4
        orch.human_challenge.assert_not_called()

    def test_non_string_text_value_rejected_not_dispatched(self) -> None:
        """Malformed payload: a non-string `value` must never be forwarded
        to human_challenge."""
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            result = discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:42", text_value=12345), "sig", "ts"
            )
        assert result["type"] == 4
        orch.human_challenge.assert_not_called()

    def test_long_challenge_text_capped_before_dispatch(self) -> None:
        """F9 fix: Discord's modal caps input at 4000 chars CLIENT-SIDE only
        (`max_length` in `_challenge_modal_response`) -- cap it server-side
        too before it reaches the CoS / planning_context."""
        orch = MagicMock()
        orch.human_challenge.return_value = "ok"
        long_text = "x" * 10_000
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message"),
        ):
            discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:42", text_value=long_text), "sig", "ts"
            )
        dispatched_text = orch.human_challenge.call_args.args[1]
        assert len(dispatched_text) == discord_bot._CHALLENGE_TEXT_MAX_LEN

    def test_human_challenge_error_reported_not_silently_swallowed(self) -> None:
        """F4 fix: only the exception TYPE name reaches chat -- the raw
        message (which could carry runner stderr, a token, or a path; see
        the known-unredacted RunResult.detail issue) must never appear.
        F7: every followup, including the error path, must carry the
        anti-mass-ping guard -- this is the assertion that would catch the
        guard being silently deleted from the error branch."""
        orch = MagicMock()
        orch.human_challenge.side_effect = RuntimeError("boom")
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:42"), "sig", "ts"
            )
        assert result == {"type": 5}
        followup.assert_called_once()
        content = followup.call_args.args[2]["content"]
        assert "RuntimeError" in content
        assert "boom" not in content
        assert followup.call_args.args[2]["allowed_mentions"] == {"parse": []}


class TestChallengeResponseChunking:
    """F8: `_MAX_MSG_LEN` + the `split_for` call in `_handle_challenge_modal_submit`
    had zero coverage -- a CoS response longer than Discord's per-message cap
    must be split into multiple ordered followups, each within the cap,
    rather than silently rejected/truncated by Discord itself."""

    def test_long_cos_response_splits_into_multiple_chunks_under_max_len(self) -> None:
        long_response = "word " * 1000  # well over _MAX_MSG_LEN (2000)
        orch = MagicMock()
        orch.human_challenge.return_value = long_response
        row = {"project": "acme", "task": "deploy"}
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=row),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(
                _modal_submit_body("challenge_modal:42", text_value="short question"),
                "sig",
                "ts",
            )
        assert followup.call_count > 1, "a long CoS response must split into >1 followup"
        for call in followup.call_args_list:
            assert len(call.args[2]["content"]) <= discord_bot._MAX_MSG_LEN
        # The final chunk (and only the final chunk) re-attaches the buttons.
        assert "components" in followup.call_args_list[-1].args[2]
        assert all("components" not in c.args[2] for c in followup.call_args_list[:-1])


# ---------------------------------------------------------------------------
# Unsupported interaction type
# ---------------------------------------------------------------------------


class TestUnsupportedInteractionType:
    def test_unknown_type_returns_generic_error(self) -> None:
        body = json.dumps(
            {"type": 99, "guild_id": ALLOWED_GUILD, "channel_id": ALLOWED_CHANNEL}
        ).encode()
        result = discord_bot.handle_interaction(body, "sig", "ts")
        assert result["type"] == 4
        assert "Unsupported" in result["data"]["content"]


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_raises_when_pynacl_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PyNaCl is not part of this environment's dependency set (optional
        `discord` extra). Other test modules (e.g. tests/test_cli.py) stub
        `nacl` into sys.modules at import time without cleanup — remove any
        such stub here so this deterministically exercises the real
        ImportError branch regardless of test execution order."""
        for mod in ("nacl", "nacl.exceptions", "nacl.signing"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        with pytest.raises(RuntimeError, match="PyNaCl required"):
            discord_bot.verify_signature(b"body", "sig", "ts")


# ---------------------------------------------------------------------------
# _format_results
# ---------------------------------------------------------------------------


class TestFormatResults:
    def test_formats_success_and_failure_rows(self) -> None:
        results = [
            types.SimpleNamespace(success=True, project="acme", target="prod", detail=None),
            types.SimpleNamespace(success=False, project="acme", target="staging", detail="boom"),
        ]
        out = discord_bot._format_results(results)
        assert "acme -> prod" in out
        assert "acme -> staging" in out
        assert "boom" in out

    def test_empty_results_returns_done(self) -> None:
        assert discord_bot._format_results([]) == "Done."


# ---------------------------------------------------------------------------
# Gateway (WebSocket) mode — fake `discord` SDK smoke test.
# ---------------------------------------------------------------------------


class _FakeIntents:
    @staticmethod
    def default() -> "_FakeIntents":
        return _FakeIntents()


class _FakeInteraction:
    def __init__(self, guild_id: int | None, channel_id: int | None) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = MagicMock()
        self.response.send_message = AsyncMock()
        self.response.defer = AsyncMock()
        self.followup = MagicMock()
        self.followup.send = AsyncMock()


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, intents: Any = None) -> None:
        self.intents = intents
        self.events: dict[str, Callable] = {}
        self.user = "FakeBotUser"
        self.ran_token: str | None = None
        _FakeClient.instances.append(self)

    def event(self, fn: Callable) -> Callable:
        self.events[fn.__name__] = fn
        return fn

    def run(self, token: str) -> None:
        self.ran_token = token


class _FakeCommandTree:
    instances: list["_FakeCommandTree"] = []

    def __init__(self, client: Any) -> None:
        self.client = client
        self.commands: dict[str, Callable] = {}
        self.synced = False
        _FakeCommandTree.instances.append(self)

    def command(self, *, name: str, description: str = "") -> Callable:
        def decorator(fn: Callable) -> Callable:
            self.commands[name] = fn
            return fn

        return decorator

    async def sync(self) -> None:
        self.synced = True


def _fake_describe(**kwargs: Any) -> Callable:
    def decorator(fn: Callable) -> Callable:
        return fn

    return decorator


class _FakeAllowedMentions:
    """Stand-in for `discord.AllowedMentions` — `.none()` returns a distinct
    sentinel instance so tests can assert every concierge `send(...)` call
    passes `allowed_mentions=discord.AllowedMentions.none()` (suppresses
    `@everyone`/`@here`/role pings from attacker-influenced text)."""

    _NONE_SENTINEL = object()

    def __init__(self, sentinel: object) -> None:
        self._sentinel = sentinel

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeAllowedMentions) and self._sentinel == other._sentinel

    @staticmethod
    def none() -> "_FakeAllowedMentions":
        return _FakeAllowedMentions(_FakeAllowedMentions._NONE_SENTINEL)


@pytest.fixture()
def fake_discord(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake `discord` package tree into sys.modules so the lazy
    `import discord` / `from discord import app_commands` in `run_gateway`
    succeeds without the real (uninstalled) dependency and without any real
    gateway connection."""
    _FakeClient.instances.clear()
    _FakeCommandTree.instances.clear()

    fake_discord_mod = types.ModuleType("discord")
    fake_discord_mod.Intents = _FakeIntents  # type: ignore[attr-defined]
    fake_discord_mod.Client = _FakeClient  # type: ignore[attr-defined]
    fake_discord_mod.Interaction = _FakeInteraction  # type: ignore[attr-defined]
    fake_discord_mod.AllowedMentions = _FakeAllowedMentions  # type: ignore[attr-defined]

    fake_app_commands = types.ModuleType("discord.app_commands")
    fake_app_commands.CommandTree = _FakeCommandTree  # type: ignore[attr-defined]
    fake_app_commands.describe = _fake_describe  # type: ignore[attr-defined]
    fake_discord_mod.app_commands = fake_app_commands  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "discord", fake_discord_mod)
    monkeypatch.setitem(sys.modules, "discord.app_commands", fake_app_commands)
    monkeypatch.setattr(discord_bot.settings, "discord_bot_token", "bot-token-test")
    return types.SimpleNamespace(Client=_FakeClient, CommandTree=_FakeCommandTree)


class TestRunGateway:
    def test_registers_all_commands_and_runs_client(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        assert len(_FakeClient.instances) == 1
        client = _FakeClient.instances[0]
        assert client.ran_token == "bot-token-test"

        tree = _FakeCommandTree.instances[0]
        assert set(tree.commands.keys()) == {"run", "approvals", "approve", "deny", "status"}

    def test_on_ready_syncs_tree(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        tree = _FakeCommandTree.instances[0]
        client = _FakeClient.instances[0]
        asyncio.run(client.events["on_ready"]())
        assert tree.synced is True

    def test_allowed_guild_channel_run_command_triggers_task(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        tree = _FakeCommandTree.instances[0]
        interaction = _FakeInteraction(guild_id=ALLOWED_GUILD, channel_id=ALLOWED_CHANNEL)
        orch = MagicMock()
        orch.run_task.return_value = []
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            asyncio.run(tree.commands["run"](interaction, "acme", "deploy", None))
        interaction.response.send_message.assert_not_called()
        interaction.response.defer.assert_awaited_once()
        orch.run_task.assert_called_once()
        interaction.followup.send.assert_awaited_once()

    def test_denied_guild_channel_run_command_rejected(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        tree = _FakeCommandTree.instances[0]
        interaction = _FakeInteraction(guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL)
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            asyncio.run(tree.commands["run"](interaction, "acme", "deploy", None))
        interaction.response.send_message.assert_awaited_once_with("Unauthorized.", ephemeral=True)
        interaction.response.defer.assert_not_awaited()
        orch.run_task.assert_not_called()

    def test_denied_guild_channel_approve_command_rejected_no_mutation(
        self, fake_discord: Any
    ) -> None:
        discord_bot.run_gateway()
        tree = _FakeCommandTree.instances[0]
        interaction = _FakeInteraction(guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL)
        orch = MagicMock()
        with patch.object(discord_bot, "_get_orch", return_value=orch):
            asyncio.run(tree.commands["approve"](interaction, 42))
        interaction.response.send_message.assert_awaited_once_with("Unauthorized.", ephemeral=True)
        orch.approve_run.assert_not_called()


# ---------------------------------------------------------------------------
# Gateway `on_interaction` -- F1 fix: Approve/Deny/Challenge buttons were
# DEAD in gateway mode before this (no interaction handler was registered at
# all beyond the slash-command CommandTree, so Discord never received an ACK
# for a MESSAGE_COMPONENT/MODAL_SUBMIT interaction and showed "This
# interaction failed"). `on_interaction` must route those two interaction
# types into the SAME `_dispatch_interaction` dict-driven dispatcher the
# HTTP-interactions webhook path (`handle_interaction`) uses -- never a
# gateway-specific reimplementation -- and deliver the computed response via
# the interaction-callback REST endpoint (gateway has no HTTP response body
# to return it in). APPLICATION_COMMAND interactions must NOT be routed here
# -- the CommandTree already auto-dispatches those.
# ---------------------------------------------------------------------------


class _FakeGatewayInteraction:
    def __init__(
        self,
        *,
        itype: int,
        data: dict[str, Any],
        guild_id: int | None = ALLOWED_GUILD,
        channel_id: int | None = ALLOWED_CHANNEL,
        application_id: str = "app-1",
        token: str = "tok-1",
        interaction_id: str = "int-1",
        username: str = "alice",
        user_id: int = 42,
    ) -> None:
        self.type = itype
        self.data = data
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.application_id = application_id
        self.token = token
        self.id = interaction_id
        self.user = types.SimpleNamespace(name=username, id=user_id)


class TestGatewayOnInteractionRoutesToSharedHandler:
    def test_component_approve_routes_through_shared_dispatch(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(itype=3, data={"custom_id": "approve:42"})
        orch = MagicMock()
        orch.approve_run.return_value = types.SimpleNamespace(success=True)
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_send_interaction_response") as send_resp,
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        # The deferred ack is delivered via the interaction-callback REST
        # endpoint -- gateway mode has no HTTP response body to return it in.
        send_resp.assert_called_once_with("int-1", "tok-1", {"type": 5})
        orch.approve_run.assert_called_once_with(run_id=42, approve=True, approver="discord")
        followup.assert_called_once()

    def test_component_denied_guild_rejected_no_mutation(self, fake_discord: Any) -> None:
        """SECURITY REGRESSION GUARD: the guild/channel allow-list must be
        enforced identically on this new gateway path -- it must NEVER be a
        weaker second door onto Approve/Deny/Challenge."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(
            itype=3,
            data={"custom_id": "approve:42"},
            guild_id=DENIED_GUILD,
            channel_id=DENIED_CHANNEL,
        )
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_send_interaction_response") as send_resp,
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        orch.approve_run.assert_not_called()
        send_resp.assert_called_once()
        sent_payload = send_resp.call_args.args[2]
        assert sent_payload["data"]["content"] == "Unauthorized."

    def test_component_challenge_button_opens_modal_via_interaction_callback(
        self, fake_discord: Any
    ) -> None:
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(itype=3, data={"custom_id": "challenge:42"})
        with patch.object(discord_bot, "_send_interaction_response") as send_resp:
            asyncio.run(client.events["on_interaction"](interaction))
        send_resp.assert_called_once_with(
            "int-1",
            "tok-1",
            {
                "type": 9,
                "data": {
                    "custom_id": "challenge_modal:42",
                    "title": "Challenge / Ask — run #42",
                    "components": [
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 4,
                                    "custom_id": "challenge_text",
                                    "style": 2,
                                    "label": "Your challenge or question",
                                    "required": True,
                                    "max_length": 4000,
                                }
                            ],
                        }
                    ],
                },
            },
        )

    def test_modal_submit_routes_through_shared_dispatch(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(
            itype=5,
            data={
                "custom_id": "challenge_modal:42",
                "components": [
                    {
                        "type": 1,
                        "components": [{"type": 4, "custom_id": "challenge_text", "value": "why?"}],
                    }
                ],
            },
        )
        orch = MagicMock()
        orch.human_challenge.return_value = "Jules says: looks fine."
        row = {"project": "acme", "task": "deploy"}
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=row),
            patch.object(discord_bot, "_send_interaction_response") as send_resp,
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        send_resp.assert_called_once_with("int-1", "tok-1", {"type": 5})
        orch.human_challenge.assert_called_once_with(42, "why?", "discord:alice")
        assert followup.called

    def test_modal_submit_denied_guild_rejected_no_dispatch(self, fake_discord: Any) -> None:
        """SECURITY REGRESSION GUARD: same allow-list enforcement for the
        MODAL_SUBMIT branch of the new gateway path."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(
            itype=5,
            data={"custom_id": "challenge_modal:42", "components": []},
            guild_id=DENIED_GUILD,
            channel_id=DENIED_CHANNEL,
        )
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_send_interaction_response") as send_resp,
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        orch.human_challenge.assert_not_called()
        sent_payload = send_resp.call_args.args[2]
        assert sent_payload["data"]["content"] == "Unauthorized."

    def test_application_command_type_not_routed_here(self, fake_discord: Any) -> None:
        """APPLICATION_COMMAND (type 2) interactions must not be handled by
        on_interaction -- the CommandTree constructed in `run_gateway`
        already auto-dispatches those; routing them here too would
        double-invoke every slash command."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(itype=2, data={"name": "run"})
        with patch.object(discord_bot, "_send_interaction_response") as send_resp:
            asyncio.run(client.events["on_interaction"](interaction))
        send_resp.assert_not_called()

    def test_ping_type_not_routed_here(self, fake_discord: Any) -> None:
        """PING (type 1) never arrives over the gateway in practice, but
        on_interaction must not blow up or respond if it somehow did."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(itype=1, data={})
        with patch.object(discord_bot, "_send_interaction_response") as send_resp:
            asyncio.run(client.events["on_interaction"](interaction))
        send_resp.assert_not_called()

    def test_direct_message_rejected_no_dispatch(self, fake_discord: Any) -> None:
        """SECURITY REGRESSION GUARD (gateway twin of
        `test_missing_guild_and_channel_treated_as_unauthorized`): a DM to the
        bot carries `guild_id=None` (there is no guild), so with a configured
        guild allow-list it must be rejected fail-closed on the GATEWAY door
        too -- a DM must never be a private side-channel onto Approve/Deny/
        Challenge. Gateway mode is the CLI default, so this is the door most
        deployments actually expose."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(
            itype=3,
            data={"custom_id": "approve:42"},
            guild_id=None,
            channel_id=None,
        )
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_send_interaction_response") as send_resp,
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        orch.approve_run.assert_not_called()
        followup.assert_not_called()
        sent_payload = send_resp.call_args.args[2]
        assert sent_payload["data"]["content"] == "Unauthorized."


# ---------------------------------------------------------------------------
# Gateway ack/followup ORDERING.
#
# On the HTTP-interactions transport the ack is written back on the still-open
# request connection, so starting the worker first is harmless. On the GATEWAY
# transport both the ack and the worker's followup are fresh outbound HTTPS
# POSTs, and Discord rejects a followup that arrives before its interaction has
# been acknowledged with `404 Unknown Webhook`. A fast-failing worker (e.g.
# `_exec_approve` on a non-pending run -- `run_approved` raises in
# microseconds) wins that race, so the operator sees NOTHING while the mutation
# was already attempted. `on_interaction` must therefore await the ack and only
# then start the worker.
# ---------------------------------------------------------------------------


class TestGatewayAckPrecedesWorker:
    def test_ack_is_delivered_before_worker_starts(self, fake_discord: Any) -> None:
        """Ordering guard: record the real call sequence. Before the split of
        `_dispatch_interaction` into a pure `_compute_interaction_response`
        plus a caller-invoked `dispatch_work`, the worker (and its followup)
        ran INSIDE `_dispatch_interaction`, i.e. strictly BEFORE the ack."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(itype=3, data={"custom_id": "approve:42"})
        calls: list[str] = []

        def _approve_run(**_kwargs: Any) -> Any:
            calls.append("work")
            return types.SimpleNamespace(success=True)

        orch = MagicMock()
        orch.approve_run.side_effect = _approve_run
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(
                discord_bot,
                "_send_interaction_response",
                side_effect=lambda *_a: calls.append("ack"),
            ),
            patch.object(
                discord_bot,
                "_followup_message",
                side_effect=lambda *_a: calls.append("followup"),
            ),
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        assert calls == ["ack", "work", "followup"]

    def test_fast_failing_approve_still_reaches_the_operator(self, fake_discord: Any) -> None:
        """The exact production symptom: approving an already-resolved run
        makes `approve_run` raise almost instantly, so the worker's followup
        is the first thing out of the process. It must still be sent AFTER the
        ack, otherwise Discord 404s it and the operator is told nothing at
        all about a mutation that was already attempted."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(itype=3, data={"custom_id": "approve:42"})
        calls: list[str] = []
        followups: list[dict[str, Any]] = []

        orch = MagicMock()
        orch.approve_run.side_effect = ValueError("Run 42 is not pending approval")

        def _record_followup(*args: Any) -> None:
            calls.append("followup")
            followups.append(args[2])

        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(
                discord_bot,
                "_send_interaction_response",
                side_effect=lambda *_a: calls.append("ack"),
            ),
            patch.object(discord_bot, "_followup_message", side_effect=_record_followup),
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        assert calls == ["ack", "followup"]
        assert followups[0]["content"].startswith("Error:")

    def test_modal_submit_ack_is_delivered_before_worker_starts(self, fake_discord: Any) -> None:
        """Same ordering guarantee on the MODAL_SUBMIT branch -- the
        Challenge/Ask worker delivers its ENTIRE payload over followup
        webhooks, so an unacknowledged interaction loses the whole CoS
        response."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(
            itype=5,
            data={
                "custom_id": "challenge_modal:42",
                "components": [
                    {
                        "type": 1,
                        "components": [{"type": 4, "custom_id": "challenge_text", "value": "why?"}],
                    }
                ],
            },
        )
        calls: list[str] = []

        def _human_challenge(*_args: Any, **_kwargs: Any) -> str:
            calls.append("work")
            return "Jules says: looks fine."

        orch = MagicMock()
        orch.human_challenge.side_effect = _human_challenge
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch(
                "hivepilot.services.state_service.get_approval",
                return_value={"project": "acme", "task": "deploy"},
            ),
            patch.object(
                discord_bot,
                "_send_interaction_response",
                side_effect=lambda *_a: calls.append("ack"),
            ),
            patch.object(
                discord_bot,
                "_followup_message",
                side_effect=lambda *_a: calls.append("followup"),
            ),
        ):
            asyncio.run(client.events["on_interaction"](interaction))
        assert calls[:2] == ["ack", "work"]

    def test_webhook_transport_ordering_is_unchanged(self) -> None:
        """The webhook path must keep its pre-existing behaviour exactly:
        `_dispatch_interaction` starts the worker itself, synchronously,
        before returning the response dict FastAPI writes back."""
        calls: list[str] = []

        def _approve_run(**_kwargs: Any) -> Any:
            calls.append("work")
            return types.SimpleNamespace(success=True)

        orch = MagicMock()
        orch.approve_run.side_effect = _approve_run
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(
                discord_bot,
                "_followup_message",
                side_effect=lambda *_a: calls.append("followup"),
            ),
        ):
            result = discord_bot.handle_interaction(_component_body("approve:42"), "sig", "ts")
        assert result == {"type": 5}
        assert calls == ["work", "followup"]

    def test_ack_failure_is_logged_and_worker_not_started(self, fake_discord: Any) -> None:
        """A `raise_for_status()` failure on the interaction-callback POST must
        not escape into discord.py's event-dispatch loop. It is logged, and the
        worker is deliberately NOT started -- its followup webhook is unusable
        without a delivered ack, so running it would mutate a run the operator
        can never be told about."""
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        interaction = _FakeGatewayInteraction(itype=3, data={"custom_id": "approve:42"})
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(
                discord_bot,
                "_send_interaction_response",
                side_effect=RuntimeError("500 Server Error"),
            ),
            patch.object(discord_bot, "_followup_message") as followup,
            patch.object(discord_bot.logger, "error") as log_error,
        ):
            # Must NOT raise.
            asyncio.run(client.events["on_interaction"](interaction))
        orch.approve_run.assert_not_called()
        followup.assert_not_called()
        assert log_error.call_args.args[0] == "discord.gateway.ack_failed"


# ---------------------------------------------------------------------------
# Natural-language concierge (opt-in, settings.chatops_concierge_enabled) —
# gateway-mode only (`run_gateway`'s `on_message`).
# ---------------------------------------------------------------------------


class _FakeGuild:
    def __init__(self, guild_id: int | None) -> None:
        self.id = guild_id


class _FakeChannel:
    def __init__(self, channel_id: int | None) -> None:
        self.id = channel_id
        self.send = AsyncMock()


class _FakeMessage:
    def __init__(
        self,
        content: str,
        *,
        author: Any = "OtherUser",
        guild_id: int | None = ALLOWED_GUILD,
        channel_id: int | None = ALLOWED_CHANNEL,
    ) -> None:
        self.content = content
        self.author = author
        self.guild = _FakeGuild(guild_id) if guild_id is not None else None
        self.channel = _FakeChannel(channel_id)


class TestGatewayMessageContentIntent:
    def test_intent_not_set_when_concierge_disabled(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        assert getattr(client.intents, "message_content", None) is not True

    def test_intent_set_when_concierge_enabled(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        assert client.intents.message_content is True


class TestOnMessageConciergeFlagOff:
    def test_flag_off_route_never_called_no_message_sent(self, fake_discord: Any) -> None:
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("hello there")
        with patch("hivepilot.services.concierge_service.route") as route:
            asyncio.run(client.events["on_message"](message))
        route.assert_not_called()
        message.channel.send.assert_not_awaited()


class TestOnMessageConciergeNoLoop:
    def test_own_message_ignored(self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("hello there", author=client.user)
        with patch("hivepilot.services.concierge_service.route") as route:
            asyncio.run(client.events["on_message"](message))
        route.assert_not_called()
        message.channel.send.assert_not_awaited()

    def test_empty_content_ignored(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("   ")
        with patch("hivepilot.services.concierge_service.route") as route:
            asyncio.run(client.events["on_message"](message))
        route.assert_not_called()
        message.channel.send.assert_not_awaited()


class TestOnMessageConciergeWhitelist:
    def test_denied_guild_channel_route_never_called(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("hello there", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL)
        with patch("hivepilot.services.concierge_service.route") as route:
            asyncio.run(client.events["on_message"](message))
        route.assert_not_called()
        message.channel.send.assert_not_awaited()


class TestOnMessageConciergeAnswer:
    def test_answer_decision_sends_text(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("how's it going?")
        decision = ConciergeDecision(kind="answer", answer_text="It's running fine.")
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            asyncio.run(client.events["on_message"](message))
        message.channel.send.assert_awaited_once_with(
            "It's running fine.", allowed_mentions=discord_bot._no_mentions()
        )


class TestOnMessageConciergeDestructive:
    def test_destructive_route_sends_confirmation_and_stores_pending(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("ask gustave to fix bug")
        decision = ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix bug", destructive=True
        )
        with patch("hivepilot.services.concierge_service.route", return_value=decision):
            asyncio.run(client.events["on_message"](message))

        message.channel.send.assert_awaited_once()
        sent_text = message.channel.send.call_args.args[0]
        assert "yes " in sent_text
        assert (
            message.channel.send.call_args.kwargs["allowed_mentions"] == discord_bot._no_mentions()
        )

        assert ALLOWED_CHANNEL in discord_bot._pending_concierge
        token, stored_decision = discord_bot._pending_concierge[ALLOWED_CHANNEL]
        assert stored_decision is decision
        assert token in sent_text


class TestOnMessageConciergeYesNo:
    def _pending_route_decision(self) -> ConciergeDecision:
        return ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="fix bug", destructive=True
        )

    def test_yes_correct_token_executes_via_shared_entrypoint(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        discord_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("yes tok123")
        with patch(
            "hivepilot.services.chatops_service._execute_concierge_decision",
            return_value="Triggered task on acme",
        ) as execute:
            asyncio.run(client.events["on_message"](message))
        execute.assert_called_once()
        args = execute.call_args.args
        assert args[1] is decision
        assert args[2] == f"discord:{ALLOWED_CHANNEL}"
        message.channel.send.assert_awaited_once_with(
            "Triggered task on acme", allowed_mentions=discord_bot._no_mentions()
        )
        assert ALLOWED_CHANNEL not in discord_bot._pending_concierge

    def test_yes_wrong_token_not_executed_pending_untouched(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        discord_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("yes stale-token")
        with patch("hivepilot.services.chatops_service._execute_concierge_decision") as execute:
            asyncio.run(client.events["on_message"](message))
        execute.assert_not_called()
        message.channel.send.assert_awaited_once()
        assert "expired" in message.channel.send.call_args.args[0].lower()
        assert (
            message.channel.send.call_args.kwargs["allowed_mentions"] == discord_bot._no_mentions()
        )
        assert discord_bot._pending_concierge[ALLOWED_CHANNEL] == ("tok123", decision)

    def test_overwrite_scenario_stale_token_never_executes_new_decision(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """decision A is pending with token A; before the user replies
        "yes <token_a>", decision B overwrites the pending entry for the
        same channel (different token, different content). Replying with
        A's stale token must execute NOTHING — never A, and never B."""
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        decision_a = self._pending_route_decision()
        decision_b = ConciergeDecision(
            kind="action",
            action="run_pipeline",
            target="acme-api",
            params={"pipeline": "company"},
            destructive=True,
        )
        discord_bot._pending_concierge[ALLOWED_CHANNEL] = ("token_a", decision_a)
        # A newer destructive message overwrites the pending entry before the
        # user replies to A's confirmation prompt.
        discord_bot._pending_concierge[ALLOWED_CHANNEL] = ("token_b", decision_b)

        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("yes token_a")  # A's stale token
        with patch("hivepilot.services.chatops_service._execute_concierge_decision") as execute:
            asyncio.run(client.events["on_message"](message))
        execute.assert_not_called()
        message.channel.send.assert_awaited_once()
        assert "expired" in message.channel.send.call_args.args[0].lower()
        # B is still pending, untouched, and can still be confirmed correctly later.
        assert discord_bot._pending_concierge[ALLOWED_CHANNEL] == ("token_b", decision_b)

    def test_yes_denied_channel_falls_through_no_pending_for_that_channel(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A denied guild/channel never reaches the confirmation logic at
        all — the whitelist gate runs before pending lookup."""
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        discord_bot._pending_concierge[DENIED_CHANNEL] = ("tok123", decision)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("yes tok123", guild_id=DENIED_GUILD, channel_id=DENIED_CHANNEL)
        with patch("hivepilot.services.chatops_service._execute_concierge_decision") as execute:
            asyncio.run(client.events["on_message"](message))
        execute.assert_not_called()
        message.channel.send.assert_not_awaited()
        assert DENIED_CHANNEL in discord_bot._pending_concierge

    def test_no_cancels_and_pops(self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        decision = self._pending_route_decision()
        discord_bot._pending_concierge[ALLOWED_CHANNEL] = ("tok123", decision)
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        message = _FakeMessage("no")
        asyncio.run(client.events["on_message"](message))
        message.channel.send.assert_awaited_once_with(
            "Cancelled.", allowed_mentions=discord_bot._no_mentions()
        )
        assert ALLOWED_CHANNEL not in discord_bot._pending_concierge


# ---------------------------------------------------------------------------
# `/approve` / `/deny` (and the Approve/Deny buttons) now go through the
# shared `Orchestrator.approve_run` helper instead of calling `run_approved`
# directly -- regression coverage for the same pipeline-checkpoint KeyError
# bug on the Discord channel.
# ---------------------------------------------------------------------------


class _FakeApprovalOrchestrator:
    """Real `Orchestrator.approve_run` bound to fake `resume_pipeline`/
    `run_approved` -- exercises the ACTUAL routing method through the
    Discord handler, not a re-implementation of it."""

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


class TestDiscordApprovalRoutingThroughSharedHelper:
    def test_pipeline_checkpoint_approval_routes_to_resume_pipeline(self) -> None:
        """Live-bug regression on the Discord channel: approving a
        pipeline-checkpoint run via the `/approve` command must route to
        `resume_pipeline`, never `run_approved`, and must not raise."""
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(discord_bot, "_get_orch", return_value=fake_orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_command_body("approve", {"run_id": 7}), "sig", "ts")
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.run_approved_calls == []
        followup.assert_called_once()

    def test_per_task_approval_still_routes_to_run_approved(self) -> None:
        """A plain per-task approval via the `/approve` command must keep
        routing to `run_approved` -- unchanged behavior."""
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_per_task_approval(),
            ),
            patch.object(discord_bot, "_get_orch", return_value=fake_orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_command_body("approve", {"run_id": 8}), "sig", "ts")
        assert len(fake_orch.run_approved_calls) == 1
        assert fake_orch.resume_pipeline_calls == []
        followup.assert_called_once()

    def test_deny_pipeline_checkpoint_routes_to_resume_pipeline(self) -> None:
        """Denying a pipeline checkpoint via the `/deny` command must also
        route to `resume_pipeline` (approve=False), not `run_approved`."""
        fake_orch = _FakeApprovalOrchestrator()
        with (
            patch(
                "hivepilot.orchestrator.state_service.get_approval",
                return_value=_pipeline_checkpoint_approval(),
            ),
            patch.object(discord_bot, "_get_orch", return_value=fake_orch),
            patch.object(discord_bot, "_followup_message"),
        ):
            discord_bot.handle_interaction(
                _command_body("deny", {"run_id": 9, "reason": "not ready"}), "sig", "ts"
            )
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.resume_pipeline_calls[0]["approve"] is False
        assert fake_orch.run_approved_calls == []

    def test_no_direct_run_approved_call_in_discord_bot_source(self) -> None:
        """Static guard: the routing decision must live in ONE place
        (`Orchestrator.approve_run`) -- `discord_bot.py` must never call
        `run_approved`/`resume_pipeline` directly again for the
        approve/deny routing decision."""
        from pathlib import Path

        source = Path(discord_bot.__file__).read_text()
        assert ".run_approved(" not in source
        assert ".resume_pipeline(" not in source
        assert ".approve_run(" in source


class TestOnMessageConciergeOfferScoping:
    """A pending follow-up offer must be bound to the conversation AND to the
    person who was asked (see concierge_service's "Pending follow-up offers"),
    so a colleague's unrelated "yes" in a shared channel can never fire it."""

    def _route_kwargs(self, message: Any) -> dict[str, Any]:
        discord_bot.run_gateway()
        client = _FakeClient.instances[0]
        decision = ConciergeDecision(kind="answer", answer_text="ok")
        with patch(
            "hivepilot.services.concierge_service.route", return_value=decision
        ) as mock_route:
            asyncio.run(client.events["on_message"](message))
        mock_route.assert_called_once()
        return dict(mock_route.call_args.kwargs)

    def test_channel_and_author_threaded_into_route(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        message = _FakeMessage("oui", author=types.SimpleNamespace(id=9001))

        kwargs = self._route_kwargs(message)

        assert kwargs.get("conversation_id") == f"discord:{ALLOWED_CHANNEL}"
        assert kwargs.get("user_id") == "9001"

    def test_author_without_id_disables_offers(
        self, fake_discord: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No author id means no owner to bind an offer to — fail closed."""
        monkeypatch.setattr(discord_bot.settings, "chatops_concierge_enabled", True)
        message = _FakeMessage("oui", author=types.SimpleNamespace())

        kwargs = self._route_kwargs(message)

        assert "user_id" in kwargs  # explicitly passed, not merely absent
        assert kwargs["user_id"] is None
