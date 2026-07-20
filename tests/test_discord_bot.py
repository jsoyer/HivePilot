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
    def test_allowed_calls_run_approved(self) -> None:
        orch = MagicMock()
        orch.run_approved.return_value = types.SimpleNamespace(success=True)
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_command_body("approve", {"run_id": 42}), "sig", "ts")
        orch.run_approved.assert_called_once_with(run_id=42, approve=True, approver="discord")
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
        orch.run_approved.assert_not_called()
        followup.assert_not_called()


class TestCmdDeny:
    def test_allowed_calls_run_approved_with_deny(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(
                _command_body("deny", {"run_id": 42, "reason": "not ready"}), "sig", "ts"
            )
        orch.run_approved.assert_called_once_with(
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
        orch.run_approved.assert_not_called()
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
    def test_allowed_approve_calls_run_approved(self) -> None:
        orch = MagicMock()
        orch.run_approved.return_value = types.SimpleNamespace(success=True)
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            result = discord_bot.handle_interaction(_component_body("approve:42"), "sig", "ts")
        assert result == {"type": 5}
        orch.run_approved.assert_called_once_with(run_id=42, approve=True, approver="discord")
        followup.assert_called_once()

    def test_allowed_deny_calls_run_approved(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_component_body("deny:42"), "sig", "ts")
        orch.run_approved.assert_called_once()
        kwargs = orch.run_approved.call_args.kwargs
        assert kwargs["run_id"] == 42
        assert kwargs["approve"] is False
        assert "alice" in kwargs["reason"]
        followup.assert_called_once()

    def test_denied_channel_approve_button_rejected_no_state_mutation(self) -> None:
        """SECURITY REGRESSION GUARD: a button press from a non-allowlisted
        guild/channel must NOT call run_approved and must get a rejection,
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
        orch.run_approved.assert_not_called()
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
        orch.run_approved.assert_not_called()
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
        orch.run_approved.assert_not_called()
        followup.assert_not_called()

    def test_invalid_custom_id_handled_gracefully(self) -> None:
        orch = MagicMock()
        with (
            patch.object(discord_bot, "_get_orch", return_value=orch),
            patch.object(discord_bot, "_followup_message") as followup,
        ):
            discord_bot.handle_interaction(_component_body("approve:notanumber"), "sig", "ts")
        orch.run_approved.assert_not_called()
        followup.assert_called_once()
        assert "Invalid component id" in followup.call_args.args[2]["content"]


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
        orch.run_approved.assert_not_called()


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
        message.channel.send.assert_awaited_once_with("It's running fine.")


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
        message.channel.send.assert_awaited_once_with("Triggered task on acme")
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
        assert discord_bot._pending_concierge[ALLOWED_CHANNEL] == ("tok123", decision)

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
        message.channel.send.assert_awaited_once_with("Cancelled.")
        assert ALLOWED_CHANNEL not in discord_bot._pending_concierge
