"""Discord implementation of the channel-agnostic `StreamChannel` protocol --
thread-per-agent via Discord's channel-thread REST API.

Reuses the same REST call shape `hivepilot.services.discord_bot._post_message`
already uses (`POST /channels/{id}/messages`) -- no discord.py dependency for
sending -- plus `POST /channels/{id}/threads` ("thread without a starter
message", type 11 = GUILD_PUBLIC_THREAD) to create the per-agent thread. Same
registry-shaped JSON store pattern as Telegram's forum-topic registry, in its
own file (`stream_threads_discord.json`).
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

_DISCORD_API = "https://discord.com/api/v10"

# Discord's hard per-message character cap.
_DISCORD_MAX_LEN = 2000

# GUILD_PUBLIC_THREAD -- see https://discord.com/developers/docs/resources/channel
_THREAD_TYPE_PUBLIC = 11
_THREAD_AUTO_ARCHIVE_MINUTES = 1440  # 24h
_THREAD_NAME_MAX_LEN = 100  # Discord thread-name cap


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {settings.discord_bot_token}",
        "Content-Type": "application/json",
    }


class DiscordStreamChannel:
    name = "discord"
    max_len = _DISCORD_MAX_LEN

    def enabled(self) -> bool:
        return bool(settings.discord_bot_token) and bool(settings.discord_stream_channel_id)

    def _store_path(self):
        return default_thread_store_path(self.name)

    def ensure_agent_thread(self, agent_key: str, title: str) -> ThreadRef | None:
        channel_id = settings.discord_stream_channel_id
        if not channel_id:
            return None

        store = load_thread_store(self._store_path())
        if agent_key in store:
            return ThreadRef(channel=self.name, container=channel_id, thread_id=store[agent_key])

        try:
            resp = requests.post(
                f"{_DISCORD_API}/channels/{channel_id}/threads",
                headers=_headers(),
                json={
                    "name": title[:_THREAD_NAME_MAX_LEN],
                    "type": _THREAD_TYPE_PUBLIC,
                    "auto_archive_duration": _THREAD_AUTO_ARCHIVE_MINUTES,
                },
                timeout=10,
            )
            resp.raise_for_status()
            thread_id = resp.json()["id"]
            store[agent_key] = thread_id
            save_thread_store(self._store_path(), store)
            return ThreadRef(channel=self.name, container=channel_id, thread_id=thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stream.discord.thread_create_failed",
                agent_key=agent_key,
                channel_id=mask_id(channel_id),
                error=str(exc),
            )
        return None

    def send(self, thread: ThreadRef | None, text: str, *, rich: bool = False) -> None:
        channel_id = thread.container if thread is not None else settings.discord_stream_channel_id
        if not channel_id:
            raise NotConfigured("Discord stream channel not configured")

        target_id = thread.thread_id if thread is not None else channel_id
        try:
            self._post(target_id, text)
        except Exception as exc:  # noqa: BLE001
            if thread is not None and thread.thread_id is not None:
                # Never drop the message -- a dead/archived/deleted thread
                # degrades to a send into the parent channel (mirrors
                # Telegram's stale-topic self-heal posture), same content.
                logger.warning(
                    "stream.discord.thread_send_failed_retry_parent_channel",
                    channel_id=mask_id(channel_id),
                    thread_id=mask_id(thread.thread_id),
                    error=str(exc),
                )
                self._post(channel_id, text)
                return
            raise

    def _post(self, target_channel_id: Any, text: str) -> None:
        resp = requests.post(
            f"{_DISCORD_API}/channels/{target_channel_id}/messages",
            headers=_headers(),
            # Agent-authored text is untrusted from Discord's perspective --
            # suppress @everyone/@here/role pings it might happen to contain
            # (mirrors discord_bot._no_mentions()'s reasoning for the
            # concierge's own attacker-influenced text).
            json={"content": text, "allowed_mentions": {"parse": []}},
            timeout=10,
        )
        resp.raise_for_status()

    def format(self, markdown: str) -> str:
        # Discord natively renders standard Markdown (headers, **bold**,
        # *italic*, `code`, ``` blocks, - bullets, > blockquotes) in both
        # user and bot messages -- intentionally near-identity. Kept as an
        # explicit method (not skipped) so the abstraction stays uniform and
        # any future Discord-specific escaping has a single call site.
        return markdown


StreamChannelRegistry.register("discord", DiscordStreamChannel())
