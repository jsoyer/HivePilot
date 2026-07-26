"""Slack implementation of the channel-agnostic `StreamChannel` protocol --
thread-per-agent via ``chat.postMessage`` + ``thread_ts``.

Reuses the same ``requests.post`` against Slack's Web API that
`hivepilot.services.slack_bot.notify`/`notify_approval_required` already use
(no Slack SDK dependency), and the same registry-shaped JSON store pattern as
Telegram's forum-topic registry (`notification_service._load_topics`/
`_save_topics`) -- but in its OWN file (`stream_threads_slack.json`), never
touching Telegram's `stream_topics.json`.
"""

from __future__ import annotations

from typing import Any

import requests

from hivepilot.config import settings
from hivepilot.services.config_provenance import mask_id
from hivepilot.services.notification_service import NotConfigured
from hivepilot.streaming.base import (
    StreamChannelRegistry,
    ThreadRef,
    default_thread_store_path,
    load_thread_store,
    save_thread_store,
)
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"

# Slack truncates long `text` fields well before its ~40k total message-body
# limit; 3000 keeps a single chunk comfortably inside a single block/section
# some Slack UI surfaces render it into.
_SLACK_MAX_LEN = 3000


def _to_mrkdwn(markdown: str) -> str:
    """Convert a channel-neutral Markdown string into Slack's mrkdwn dialect.

    ``**bold**``/``__bold__`` -> ``*bold*`` (Slack's single-asterisk bold);
    ``# Header`` lines -> a bold line (Slack mrkdwn has no real headers).
    Everything else (inline code, bullets, blockquotes, links) is already
    close enough to Slack's dialect to pass through unchanged.
    """
    import re

    text = re.sub(r"^#{1,6}[ \t]+(.+)$", r"*\1*", markdown, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    return text


class SlackStreamChannel:
    name = "slack"
    max_len = _SLACK_MAX_LEN

    def enabled(self) -> bool:
        return bool(settings.slack_bot_token) and bool(settings.slack_stream_channel_id)

    def _store_path(self):
        return default_thread_store_path(self.name)

    def ensure_agent_thread(self, agent_key: str, title: str) -> ThreadRef | None:
        channel_id = settings.slack_stream_channel_id
        if not channel_id:
            return None

        store = load_thread_store(self._store_path())
        if agent_key in store:
            return ThreadRef(channel=self.name, container=channel_id, thread_id=store[agent_key])

        try:
            resp = requests.post(
                _SLACK_POST_URL,
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                json={"channel": channel_id, "text": f":thread: {title}"},
                timeout=5,
            )
            data = resp.json()
            if data.get("ok"):
                thread_ts = data["ts"]
                store[agent_key] = thread_ts
                save_thread_store(self._store_path(), store)
                return ThreadRef(channel=self.name, container=channel_id, thread_id=thread_ts)
            logger.warning(
                "stream.slack.thread_create_failed",
                agent_key=agent_key,
                channel_id=mask_id(channel_id),
                error=data.get("error"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stream.slack.thread_create_error",
                agent_key=agent_key,
                channel_id=mask_id(channel_id),
                error=str(exc),
            )
        return None

    def send(self, thread: ThreadRef | None, text: str, *, rich: bool = False) -> None:
        channel_id = thread.container if thread is not None else settings.slack_stream_channel_id
        if not channel_id:
            raise NotConfigured("Slack stream channel not configured")

        try:
            self._post(channel_id, text, thread_ts=thread.thread_id if thread else None)
        except Exception as exc:  # noqa: BLE001
            if thread is not None and thread.thread_id is not None:
                # Never drop the message -- a dead/deleted thread degrades to
                # a threadless send in the same channel (mirrors Telegram's
                # stale-topic self-heal posture), same content, unchanged.
                logger.warning(
                    "stream.slack.thread_send_failed_retry_threadless",
                    channel_id=mask_id(channel_id),
                    thread_id=mask_id(thread.thread_id),
                    error=str(exc),
                )
                self._post(channel_id, text, thread_ts=None)
                return
            raise

    def _post(self, channel_id: str, text: str, *, thread_ts: Any | None) -> None:
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        resp = requests.post(
            _SLACK_POST_URL,
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json=payload,
            timeout=5,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "Slack chat.postMessage failed"))

    def format(self, markdown: str) -> str:
        return _to_mrkdwn(markdown)


StreamChannelRegistry.register("slack", SlackStreamChannel())
