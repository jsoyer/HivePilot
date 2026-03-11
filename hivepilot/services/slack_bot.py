from __future__ import annotations

import os
import threading
from typing import Any

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Lazily-initialised bolt App instance (used by webhook/FastAPI mode)
_app_instance = None
_app_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _bot_token() -> str:
    token = settings.slack_bot_token or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Slack bot token not configured. "
            "Set HIVEPILOT_SLACK_BOT_TOKEN or SLACK_BOT_TOKEN."
        )
    return token


def _signing_secret() -> str:
    secret = settings.slack_signing_secret or os.environ.get("SLACK_SIGNING_SECRET")
    if not secret:
        raise RuntimeError(
            "Slack signing secret not configured. "
            "Set HIVEPILOT_SLACK_SIGNING_SECRET or SLACK_SIGNING_SECRET."
        )
    return secret


def _app_token() -> str:
    token = settings.slack_app_token or os.environ.get("SLACK_APP_TOKEN")
    if not token:
        raise RuntimeError(
            "Slack app token not configured (xapp-...). "
            "Set HIVEPILOT_SLACK_APP_TOKEN or SLACK_APP_TOKEN."
        )
    return token


def _is_allowed(channel_id: str) -> bool:
    """Return True if channel_id is whitelisted (open to all when list is empty)."""
    allowed = settings.slack_allowed_channel_ids
    if not allowed:
        return True
    return channel_id in allowed


def _get_orch():
    from hivepilot.services.chatops_service import _get_orchestrator
    return _get_orchestrator()


def _notification_channel_id() -> str | None:
    """Return the channel_id to use for proactive notifications."""
    return settings.slack_notification_channel_id


def _format_results(results) -> str:
    lines = [
        ("ok" if r.success else "fail") + f" {r.project} -> {r.target}"
        + (f"\n  {r.detail}" if r.detail else "")
        for r in results
    ]
    return "\n".join(lines) or "Done."


def _approval_blocks(run_id: int, project: str, task: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Approval required* — run #{run_id}\nProject: `{project}`\nTask: `{task}`",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"approve_{run_id}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": f"deny_{run_id}",
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _build_app():
    try:
        from slack_bolt import App
    except ImportError as exc:
        raise RuntimeError("slack-bolt is required: pip install hivepilot[slack]") from exc

    bolt_app = App(token=_bot_token(), signing_secret=_signing_secret())
    _register_handlers(bolt_app)
    return bolt_app


def _register_handlers(bolt_app) -> None:
    """Register all slash command and action handlers on a bolt App."""

    @bolt_app.command("/hp-run")
    def cmd_run(ack, command, respond, client):
        ack()
        channel_id = command.get("channel_id", "")
        if not _is_allowed(channel_id):
            respond("Unauthorized channel.")
            return
        text = (command.get("text") or "").strip()
        parts = text.split(None, 2)
        if len(parts) < 2:
            respond("Usage: /hp-run <project> <task> [instructions]")
            return
        project, task = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else None
        respond(f"Triggering `{task}` on `{project}`...")
        try:
            results = _get_orch().run_task(
                project_names=[project],
                task_name=task,
                extra_prompt=extra,
                auto_git=True,
            )
            respond(_format_results(results))
        except Exception as exc:
            logger.error("slack.cmd_run.error", error=str(exc))
            respond(f"Error: {exc}")

    @bolt_app.command("/hp-approvals")
    def cmd_approvals(ack, command, respond, client):
        ack()
        channel_id = command.get("channel_id", "")
        if not _is_allowed(channel_id):
            respond("Unauthorized channel.")
            return
        from hivepilot.services import state_service
        try:
            pending = state_service.get_pending_approvals()
        except Exception as exc:
            respond(f"Error: {exc}")
            return
        if not pending:
            respond("No pending approvals.")
            return
        for row in pending:
            blocks = _approval_blocks(row["run_id"], row["project"], row["task"])
            respond(blocks=blocks, text=f"Approval required — run #{row['run_id']}")

    @bolt_app.command("/hp-approve")
    def cmd_approve(ack, command, respond):
        ack()
        channel_id = command.get("channel_id", "")
        if not _is_allowed(channel_id):
            respond("Unauthorized channel.")
            return
        text = (command.get("text") or "").strip()
        if not text:
            respond("Usage: /hp-approve <run_id>")
            return
        try:
            run_id = int(text.split()[0])
        except ValueError:
            respond(f"Invalid run_id: {text!r}")
            return
        respond(f"Running approved task #{run_id}...")
        try:
            result = _get_orch().run_approved(run_id=run_id, approve=True, approver="slack")
            status = "succeeded" if result.success else "failed"
            respond(f"Run #{run_id} approved — {status}.")
        except Exception as exc:
            respond(f"Error: {exc}")

    @bolt_app.command("/hp-deny")
    def cmd_deny(ack, command, respond):
        ack()
        channel_id = command.get("channel_id", "")
        if not _is_allowed(channel_id):
            respond("Unauthorized channel.")
            return
        text = (command.get("text") or "").strip()
        parts = text.split(None, 1)
        if not parts:
            respond("Usage: /hp-deny <run_id> [reason]")
            return
        try:
            run_id = int(parts[0])
        except ValueError:
            respond(f"Invalid run_id: {parts[0]!r}")
            return
        reason = parts[1] if len(parts) > 1 else "Denied via Slack"
        try:
            _get_orch().run_approved(run_id=run_id, approve=False, approver="slack", reason=reason)
            respond(f"Run #{run_id} denied.")
        except Exception as exc:
            respond(f"Error: {exc}")

    @bolt_app.command("/hp-status")
    def cmd_status(ack, command, respond):
        ack()
        channel_id = command.get("channel_id", "")
        if not _is_allowed(channel_id):
            respond("Unauthorized channel.")
            return
        from hivepilot.services import state_service
        try:
            runs = state_service.list_recent_runs(limit=5)
        except Exception as exc:
            respond(f"Error: {exc}")
            return
        if not runs:
            respond("No recent runs.")
            return
        lines = [
            f"[{r['status']}] {r['project']} / {r['task']} — {r['started_at']}"
            for r in runs
        ]
        respond("Recent runs:\n" + "\n".join(lines))

    # -- Approval button actions -----------------------------------------------

    @bolt_app.action({"action_id": "^(approve|deny)_\\d+$"})
    def handle_approval_action(ack, action, body, respond):
        ack()
        action_id = action.get("action_id", "")
        try:
            verb, raw_id = action_id.rsplit("_", 1)
            run_id = int(raw_id)
        except (ValueError, AttributeError):
            respond(f"Invalid action: {action_id!r}")
            return
        approve = verb == "approve"
        user = (body.get("user") or {}).get("username") or (body.get("user") or {}).get("id", "unknown")
        try:
            result = _get_orch().run_approved(
                run_id=run_id,
                approve=approve,
                approver=f"slack:{user}",
                reason=None if approve else "Denied via Slack button",
            )
            if approve:
                outcome = "succeeded" if result.success else "failed"
                respond(f"Run #{run_id} approved by @{user} — {outcome}.")
            else:
                respond(f"Run #{run_id} denied by @{user}.")
        except Exception as exc:
            logger.error("slack.handle_approval_action.error", run_id=run_id, error=str(exc))
            respond(f"Error processing run #{run_id}: {exc}")


# ---------------------------------------------------------------------------
# Socket Mode  (RPI / NAT — no public URL needed)
# ---------------------------------------------------------------------------

def run_socket_mode() -> None:
    """Start the bot in Socket Mode. Blocking. No public URL required."""
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise RuntimeError("slack-bolt is required: pip install hivepilot[slack]") from exc

    logger.info("slack.socket_mode.start")
    bolt_app = _build_app()
    handler = SocketModeHandler(bolt_app, _app_token())
    handler.start()


# ---------------------------------------------------------------------------
# Webhook mode — FastAPI-integrated
# ---------------------------------------------------------------------------

def _get_or_init_webhook_app():
    """Lazily initialise the bolt App for FastAPI webhook mode."""
    global _app_instance
    if _app_instance is None:
        with _app_lock:
            if _app_instance is None:
                _app_instance = _build_app()
    return _app_instance


def run_webhook_mode():
    """
    Return the bolt App instance configured for FastAPI integration.
    The FastAPI endpoint calls handle_webhook_request(request).
    """
    return _get_or_init_webhook_app()


async def handle_webhook_request(request):
    """
    Process a raw Slack HTTP request from the FastAPI webhook endpoint.
    Uses SlackRequestHandler (sync wrapped in threadpool by FastAPI).
    """
    try:
        from slack_bolt.adapter.fastapi import SlackRequestHandler
    except ImportError as exc:
        raise RuntimeError("slack-bolt is required: pip install hivepilot[slack]") from exc

    bolt_app = _get_or_init_webhook_app()
    handler = SlackRequestHandler(bolt_app)
    return await handler.handle(request)


def shutdown() -> None:
    """Release the lazily-started App instance (call on FastAPI shutdown)."""
    global _app_instance
    with _app_lock:
        _app_instance = None


# ---------------------------------------------------------------------------
# Proactive notifications
# ---------------------------------------------------------------------------

def notify(message: str) -> None:
    """Send a plain text message to the notification channel."""
    channel_id = _notification_channel_id()
    if not channel_id:
        raise RuntimeError("No Slack notification channel_id configured (HIVEPILOT_SLACK_NOTIFICATION_CHANNEL_ID)")
    try:
        import requests as _requests
        token = _bot_token()
        resp = _requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel_id, "text": message},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("slack.notify.error", error=str(exc))
        raise


def notify_approval_required(*, run_id: int, project: str, task: str) -> None:
    """
    Send a Block Kit approval message to the notification channel (sync, fire-and-forget).
    Called from notification_service — safe to call from non-async context.
    """
    channel_id = _notification_channel_id()
    if not channel_id:
        raise RuntimeError("No Slack notification channel_id configured (HIVEPILOT_SLACK_NOTIFICATION_CHANNEL_ID)")

    token = _bot_token()
    blocks = _approval_blocks(run_id=run_id, project=project, task=task)
    try:
        import requests as _requests
        resp = _requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel": channel_id,
                "text": f"Approval required — run #{run_id}",
                "blocks": blocks,
            },
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("slack.notify_approval_required.error", run_id=run_id, error=str(exc))
        raise
