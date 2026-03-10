from __future__ import annotations

import os
from collections.abc import Iterable

import requests

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


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


def _send_telegram(message: str) -> None:
    from hivepilot.config import settings
    token = settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = settings.telegram_notification_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id and settings.telegram_allowed_chat_ids:
        chat_id = settings.telegram_allowed_chat_ids[0]
    if not token or not chat_id:
        raise _NotConfigured("Telegram not configured (token or notification chat_id missing)")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=5)


def send_approval_keyboard(run_id: int, project: str, task: str) -> None:
    """Send an approval request with inline Approve/Deny buttons via Telegram."""
    try:
        from hivepilot.services.telegram_bot import notify_approval_required
        notify_approval_required(run_id=run_id, project=project, task=task)
    except _NotConfigured:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("notification.approval_keyboard.failed", error=str(exc))
        # Fallback to plain text
        send_notification(f"Approval required for run #{run_id}: {project} → {task}")
