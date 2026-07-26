"""Telegram implementation of the channel-agnostic `StreamChannel` protocol.

Thin adapter over `hivepilot.services.notification_service`'s existing,
fully-tested Telegram streaming pipeline (forum-topic-per-agent registry,
stale/closed-topic self-heal, entity-aware HTML chunk splitting). That
pipeline is deliberately NOT relocated here -- see `hivepilot.streaming.base`'s
module docstring: its tests patch `notification_service.<name>` module
attributes directly (`_ensure_topic_thread`, `_send_telegram`, `_send_chunks`,
`settings`, ...), and `notification_service.stream_agent_turn`'s own Telegram
delivery keeps calling those same functions verbatim (see
`notification_service._stream_agent_turn_telegram`) for guaranteed
byte-identical behavior. Every method below therefore reaches back into
`notification_service` via attribute access on the module object itself
(``ns.<name>``, never a bare `from ... import <name>`) so it always sees
whatever is currently bound there -- including anything a test monkeypatched.
"""

from __future__ import annotations

from hivepilot.services import notification_service as ns
from hivepilot.streaming.base import StreamChannelRegistry, ThreadRef


class TelegramStreamChannel:
    name = "telegram"
    # Telegram's hard message cap; notification_service._SPLIT_LIMIT (used by
    # the dedicated pipeline this adapter delegates to) keeps headroom below
    # it for entity open/reopen overhead across a chunk boundary.
    max_len = 4096

    def enabled(self) -> bool:
        return bool(ns.settings.telegram_stream_live)

    def ensure_agent_thread(self, agent_key: str, title: str) -> ThreadRef | None:
        if not (ns.settings.telegram_stream_topics and ns.settings.telegram_stream_chat_id):
            return None
        thread_id = ns._ensure_topic_thread(agent_key, title)
        if thread_id is None:
            return None
        return ThreadRef(
            channel=self.name,
            container=ns.settings.telegram_stream_chat_id,
            thread_id=thread_id,
        )

    def send(self, thread: ThreadRef | None, text: str, *, rich: bool = False) -> None:
        chat_id = thread.container if thread is not None else ns.settings.telegram_stream_chat_id
        message_thread_id = thread.thread_id if thread is not None else None
        ns._send_chunks(
            text,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            parse_mode="HTML" if rich else None,
            html_aware=rich,
        )

    def format(self, markdown: str) -> str:
        return ns._format_for_telegram_html(markdown)


StreamChannelRegistry.register("telegram", TelegramStreamChannel())
