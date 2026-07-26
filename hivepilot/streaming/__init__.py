"""Built-in stream channels (multi-channel agent-streaming sprint).

Importing this package registers every built-in `StreamChannel` (Telegram,
Slack, Discord) into `hivepilot.streaming.base.STREAM_CHANNEL_MAP`, mirroring
`hivepilot/graph_sources/__init__.py`'s registration pattern. Idempotent --
safe to import more than once in the same process. Imported LAZILY (from
`notification_service.stream_agent_turn`, not at that module's top level) to
avoid a Telegram-channel -> notification_service -> streaming import cycle
(`telegram_channel.py` imports `notification_service` itself).
"""

from __future__ import annotations

from hivepilot.streaming import discord_channel as _discord_channel  # noqa: F401
from hivepilot.streaming import slack_channel as _slack_channel  # noqa: F401
from hivepilot.streaming import telegram_channel as _telegram_channel  # noqa: F401
