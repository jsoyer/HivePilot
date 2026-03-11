from __future__ import annotations

import json
import threading
from typing import Any

import requests

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_DISCORD_API = "https://discord.com/api/v10"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    token = settings.discord_bot_token
    if not token:
        raise RuntimeError(
            "Discord bot token not configured. "
            "Set HIVEPILOT_DISCORD_BOT_TOKEN."
        )
    return token


def _is_allowed(guild_id: int | None, channel_id: int | None) -> bool:
    """Return True when the request originates from an allowed guild/channel.

    Guild whitelist: open to all when discord_allowed_guild_ids is empty.
    Channel whitelist: open to all when discord_allowed_channel_ids is empty.
    """
    allowed_guilds = settings.discord_allowed_guild_ids
    if allowed_guilds and guild_id not in allowed_guilds:
        return False
    allowed_channels = settings.discord_allowed_channel_ids
    if allowed_channels and channel_id not in allowed_channels:
        return False
    return True


def _get_orch():
    from hivepilot.services.chatops_service import _get_orchestrator
    return _get_orchestrator()


def _format_results(results) -> str:
    lines = [
        ("+ " if r.success else "- ") + f"{r.project} -> {r.target}"
        + (f"\n  {r.detail}" if r.detail else "")
        for r in results
    ]
    return "\n".join(lines) or "Done."


# ---------------------------------------------------------------------------
# Discord REST helpers
# ---------------------------------------------------------------------------

def _bot_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {_token()}",
        "Content-Type": "application/json",
    }


def _post_message(channel_id: int, payload: dict[str, Any]) -> None:
    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    resp = requests.post(url, headers=_bot_headers(), json=payload, timeout=10)
    resp.raise_for_status()


def _followup_message(application_id: str, interaction_token: str, payload: dict[str, Any]) -> None:
    url = f"{_DISCORD_API}/webhooks/{application_id}/{interaction_token}"
    resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=10)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Signature verification (Ed25519 via PyNaCl)
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str, timestamp: str) -> bool:
    """Verify the Ed25519 signature sent by Discord on every interaction."""
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        raise RuntimeError("PyNaCl required: pip install hivepilot[discord]")
    if not settings.discord_public_key:
        raise RuntimeError(
            "Discord public key not configured. Set HIVEPILOT_DISCORD_PUBLIC_KEY."
        )
    key = VerifyKey(bytes.fromhex(settings.discord_public_key))
    try:
        key.verify((timestamp + body.decode()).encode(), bytes.fromhex(signature))
        return True
    except BadSignatureError:
        return False


# ---------------------------------------------------------------------------
# Command logic (shared between gateway and HTTP modes)
# ---------------------------------------------------------------------------

def _exec_run(project: str, task: str, extra: str | None) -> str:
    try:
        results = _get_orch().run_task(
            project_names=[project],
            task_name=task,
            extra_prompt=extra,
            auto_git=True,
        )
        return _format_results(results)
    except Exception as exc:
        logger.error("discord.cmd_run.error", error=str(exc))
        return f"Error: {exc}"


def _exec_approvals() -> str:
    from hivepilot.services import state_service
    try:
        pending = state_service.get_pending_approvals()
    except Exception as exc:
        return f"Error: {exc}"
    if not pending:
        return "No pending approvals."
    lines = [
        f"#{r['run_id']} — {r['project']} / {r['task']}"
        for r in pending
    ]
    return "Pending approvals:\n" + "\n".join(lines)


def _exec_approve(run_id: int) -> str:
    try:
        result = _get_orch().run_approved(run_id=run_id, approve=True, approver="discord")
        status = "succeeded" if result.success else "failed"
        return f"Run #{run_id} approved — {status}."
    except Exception as exc:
        return f"Error: {exc}"


def _exec_deny(run_id: int, reason: str) -> str:
    try:
        _get_orch().run_approved(run_id=run_id, approve=False, approver="discord", reason=reason)
        return f"Run #{run_id} denied."
    except Exception as exc:
        return f"Error: {exc}"


def _exec_status() -> str:
    from hivepilot.services import state_service
    try:
        runs = state_service.list_recent_runs(limit=5)
    except Exception as exc:
        return f"Error: {exc}"
    if not runs:
        return "No recent runs."
    lines = [
        f"[{r['status']}] {r['project']} / {r['task']} — {r['started_at']}"
        for r in runs
    ]
    return "Recent runs:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP interactions mode
# ---------------------------------------------------------------------------

def _handle_component(interaction: dict[str, Any], application_id: str, interaction_token: str) -> None:
    """Process a button component interaction in a background thread."""
    custom_id = interaction.get("data", {}).get("custom_id", "")
    try:
        action, raw_id = custom_id.split(":", 1)
        run_id = int(raw_id)
    except (ValueError, AttributeError):
        _followup_message(application_id, interaction_token, {"content": f"Invalid component id: {custom_id!r}"})
        return

    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    approver = user.get("username") or str(user.get("id", "discord"))

    if action == "approve":
        msg = _exec_approve(run_id)
    elif action == "deny":
        msg = _exec_deny(run_id, f"Denied via Discord button by {approver}")
    else:
        msg = f"Unknown action: {action!r}"

    _followup_message(application_id, interaction_token, {"content": msg})


def handle_interaction(body: bytes, signature: str, timestamp: str) -> dict[str, Any]:
    """
    Process a raw Discord interaction from the FastAPI webhook endpoint.
    Returns a JSON-serialisable dict for FastAPI to return directly.
    """
    data: dict[str, Any] = json.loads(body)
    interaction_type = data.get("type", 0)

    # PING — Discord health check
    if interaction_type == 1:
        return {"type": 1}

    application_id = str(data.get("application_id", ""))
    interaction_token = data.get("token", "")

    guild_id = int(data["guild_id"]) if data.get("guild_id") else None
    channel_id = int(data["channel_id"]) if data.get("channel_id") else None

    if not _is_allowed(guild_id, channel_id):
        logger.warning("discord.unauthorized", guild_id=guild_id, channel_id=channel_id)
        return {
            "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
            "data": {"content": "Unauthorized.", "flags": 64},
        }

    # MESSAGE_COMPONENT (button)
    if interaction_type == 3:
        threading.Thread(
            target=_handle_component,
            args=(data, application_id, interaction_token),
            daemon=True,
        ).start()
        return {"type": 5}  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

    # APPLICATION_COMMAND
    if interaction_type == 2:
        cmd_data = data.get("data", {})
        cmd_name = cmd_data.get("name", "")
        options: dict[str, Any] = {
            opt["name"]: opt.get("value") for opt in cmd_data.get("options", [])
        }

        def _dispatch() -> None:
            try:
                if cmd_name == "run":
                    project = options.get("project", "")
                    task = options.get("task", "")
                    instructions = options.get("instructions")
                    msg = _exec_run(project, task, instructions)
                elif cmd_name == "approvals":
                    msg = _exec_approvals()
                elif cmd_name == "approve":
                    try:
                        run_id = int(options.get("run_id", 0))
                    except (TypeError, ValueError):
                        msg = "Invalid run_id."
                    else:
                        msg = _exec_approve(run_id)
                elif cmd_name == "deny":
                    try:
                        run_id = int(options.get("run_id", 0))
                    except (TypeError, ValueError):
                        msg = "Invalid run_id."
                    else:
                        reason = options.get("reason") or "Denied via Discord"
                        msg = _exec_deny(run_id, reason)
                elif cmd_name == "status":
                    msg = _exec_status()
                else:
                    msg = f"Unknown command: {cmd_name!r}"
            except Exception as exc:
                logger.error("discord.http.dispatch.error", cmd=cmd_name, error=str(exc))
                msg = f"Error: {exc}"
            _followup_message(application_id, interaction_token, {"content": msg})

        threading.Thread(target=_dispatch, daemon=True).start()
        return {"type": 5}  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

    return {"type": 4, "data": {"content": "Unsupported interaction type.", "flags": 64}}


# ---------------------------------------------------------------------------
# Proactive notifications (REST, synchronous)
# ---------------------------------------------------------------------------

def notify_approval_required(*, run_id: int, project: str, task: str) -> None:
    """Post an approval embed with Approve/Deny buttons to the notification channel."""
    channel_id = settings.discord_notification_channel_id
    if not channel_id:
        raise RuntimeError("No Discord notification channel_id configured")

    payload: dict[str, Any] = {
        "embeds": [{
            "title": "Approval required",
            "description": f"**Run #{run_id}**\nProject: `{project}`\nTask: `{task}`",
            "color": 0xFFA500,
        }],
        "components": [{
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Approve",
                    "custom_id": f"approve:{run_id}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Deny",
                    "custom_id": f"deny:{run_id}",
                },
            ],
        }],
    }
    _post_message(channel_id, payload)


def notify(message: str) -> None:
    """Send a plain text message to the notification channel via REST."""
    channel_id = settings.discord_notification_channel_id
    if not channel_id:
        raise RuntimeError("No Discord notification channel_id configured")
    _post_message(channel_id, {"content": message})


# ---------------------------------------------------------------------------
# Gateway mode  (discord.py — blocking, no public URL needed)
# ---------------------------------------------------------------------------

def run_gateway() -> None:
    """Start the bot in gateway (WebSocket) mode. Blocking. No public URL required."""
    try:
        import discord
        from discord import app_commands
    except ImportError as exc:
        raise RuntimeError("discord.py required: pip install hivepilot[discord]") from exc

    token = _token()

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    def _guild_check(interaction: discord.Interaction) -> bool:
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        return _is_allowed(guild_id, channel_id)

    @tree.command(name="run", description="Trigger a HivePilot task")
    @app_commands.describe(
        project="Project name",
        task="Task name",
        instructions="Optional extra instructions",
    )
    async def cmd_run(
        interaction: discord.Interaction,
        project: str,
        task: str,
        instructions: str | None = None,
    ) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, lambda: _exec_run(project, task, instructions))
        await interaction.followup.send(msg)

    @tree.command(name="approvals", description="List pending approvals")
    async def cmd_approvals(interaction: discord.Interaction) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _exec_approvals)
        await interaction.followup.send(msg)

    @tree.command(name="approve", description="Approve a pending run")
    @app_commands.describe(run_id="Run ID to approve")
    async def cmd_approve(interaction: discord.Interaction, run_id: int) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, lambda: _exec_approve(run_id))
        await interaction.followup.send(msg)

    @tree.command(name="deny", description="Deny a pending run")
    @app_commands.describe(run_id="Run ID to deny", reason="Optional reason")
    async def cmd_deny(
        interaction: discord.Interaction,
        run_id: int,
        reason: str | None = None,
    ) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio
        loop = asyncio.get_event_loop()
        effective_reason = reason or "Denied via Discord"
        msg = await loop.run_in_executor(None, lambda: _exec_deny(run_id, effective_reason))
        await interaction.followup.send(msg)

    @tree.command(name="status", description="Show last 5 runs")
    async def cmd_status(interaction: discord.Interaction) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _exec_status)
        await interaction.followup.send(msg)

    @client.event
    async def on_ready() -> None:
        await tree.sync()
        logger.info("discord.gateway.ready", user=str(client.user))

    logger.info("discord.gateway.start")
    client.run(token)
