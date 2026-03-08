from __future__ import annotations

import os
from typing import Iterable, Optional

import requests

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def send_notification(message: str, channels: Optional[Iterable[str]] = None) -> None:
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("notification.failed", channel=channel, error=str(exc))


def _send_slack(message: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    requests.post(webhook, json={"text": message}, timeout=5)


def _send_discord(message: str) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    requests.post(webhook, json={"content": message}, timeout=5)


def _send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=5)
