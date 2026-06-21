from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import requests

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Max chars of an agent's output shown inline in a Telegram turn (Telegram caps
# messages at ~4096; keep headroom for the header lines).
_STREAM_MAX_CHARS = 1500

# Human-readable meaning shown next to each live-stream emoji.
_ICON_LABELS = {
    "🚀": "start",
    "🗣": "hand-off",
    "⏸️": "approval needed",
    "💬": "proposal",
    "⚖️": "synthesis",
}


def send_notification(message: str, channels: Iterable[str] | None = None) -> None:
    channels = list(channels) if channels else ["slack", "discord", "telegram"]
    for channel in channels:
        channel = channel.lower()
        try:
            if channel == "slack":
                _send_slack(message)
            elif channel == "discord":
                _send_discord(message)
            elif channel == "telegram":
                _send_telegram(message)
        except _NotConfigured:
            pass  # silently skip unconfigured channels
        except Exception as exc:  # noqa: BLE001
            logger.warning("notification.failed", channel=channel, error=str(exc))


def emit_event(event: str, **fields: Any) -> None:
    """POST a structured pipeline-lifecycle event to the configured webhook (n8n,
    Zapier, a dashboard, …). Best-effort and a silent no-op when no webhook is set
    — it must never break a run. Payload: ``{"event": <event>, **fields}``."""
    url = settings.event_webhook_url
    if not url:
        return
    payload: dict[str, Any] = {"event": event, **fields}
    headers = {}
    if settings.event_webhook_token:
        headers["Authorization"] = f"Bearer {settings.event_webhook_token}"
    try:
        requests.post(url, json=payload, headers=headers, timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("event.emit_failed", event=event, error=str(exc))


class _NotConfigured(Exception):
    """Raised when a notification channel has no credentials configured."""


def _send_slack(message: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        raise _NotConfigured("SLACK_WEBHOOK_URL not set")
    requests.post(webhook, json={"text": message}, timeout=5)


def _send_discord(message: str) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise _NotConfigured("DISCORD_WEBHOOK_URL not set")
    requests.post(webhook, json={"content": message}, timeout=5)


def _send_telegram(message: str, chat_id: int | str | None = None) -> None:

    token = settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = (
        chat_id or settings.telegram_notification_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    )
    if not chat_id and settings.telegram_allowed_chat_ids:
        chat_id = settings.telegram_allowed_chat_ids[0]
    if not token or not chat_id:
        raise _NotConfigured("Telegram not configured (token or notification chat_id missing)")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=5)


def stream_agent_turn(
    *,
    actor: str,
    stage: str | None = None,
    target: str | None = None,
    summary: str | None = None,
    icon: str = "🗣",
) -> None:
    """Live-stream a single agent's turn to Telegram (outbound ``sendMessage`` only).

    Used during pipeline and debate runs so the user can watch the agents talk
    in real time. Intentionally Telegram-only (the live channel) and a silent
    no-op when streaming is disabled or Telegram is unconfigured — it must never
    break a run.
    """
    if not settings.telegram_stream_live:
        return
    label = _ICON_LABELS.get(icon)
    tag = f"{icon} ({label})" if label else icon
    header = f"{tag} {actor}" + (f" — {stage}" if stage else "")
    lines = [header]
    if target:
        lines.append(f"   ↳ {target}")
    if summary:
        snippet = " ".join(summary.split())
        if len(snippet) > _STREAM_MAX_CHARS:
            snippet = snippet[: _STREAM_MAX_CHARS - 1] + "…"
        if snippet:
            lines.append(f"   {snippet}")
    try:
        # Live agent stream goes to its dedicated channel when set, else falls
        # back to the main notification chat.
        _send_telegram("\n".join(lines), chat_id=settings.telegram_stream_chat_id)
    except _NotConfigured:
        pass  # Telegram not set up — streaming is best-effort
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.failed", error=str(exc))


def send_approval_keyboard(run_id: int, project: str, task: str) -> None:
    """Send an approval request with inline Approve/Deny buttons via Telegram and Slack."""
    try:
        from hivepilot.services.telegram_bot import notify_approval_required

        notify_approval_required(run_id=run_id, project=project, task=task)
    except _NotConfigured:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("notification.approval_keyboard.failed", channel="telegram", error=str(exc))
        # Fallback to plain text
        send_notification(f"Approval required for run #{run_id}: {project} -> {task}")

    try:
        from hivepilot.services.slack_bot import notify_approval_required as slack_notify

        slack_notify(run_id=run_id, project=project, task=task)
    except Exception:  # noqa: BLE001
        pass

    try:
        from hivepilot.services.discord_bot import notify_approval_required as discord_notify

        discord_notify(run_id=run_id, project=project, task=task)
    except Exception:  # noqa: BLE001
        pass
