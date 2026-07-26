"""Channel-agnostic live agent-stream abstraction.

`hivepilot.services.notification_service.stream_agent_turn` fans an agent
turn out to every ENABLED `StreamChannel` -- Telegram, Slack, Discord, or a
future plugin-registered channel -- each delivering into its own native
"one thread/topic per agent" container:

| | Telegram | Slack | Discord |
|---|---|---|---|
| per-agent container | forum **topic** (`message_thread_id`) | **thread** on a parent message (`thread_ts`) | **thread** in a channel |
| formatting | HTML subset | mrkdwn | markdown |
| length cap | ~4096 | ~3000 | 2000 |

`STREAM_CHANNEL_MAP` + `StreamChannelRegistry` mirror
`notification_service.NOTIFIER_MAP`/`NotifierRegistry`: a process-global
map, fail-closed on a name collision (a plugin can register a new channel,
but never silently clobber a built-in without `override=True`).

Telegram's own pipeline (forum-topic registry, stale/closed-topic self-heal,
entity-aware HTML formatting/splitting) is NOT relocated here -- it stays in
`notification_service.py`, byte-identical, because its existing test suite
patches `hivepilot.services.notification_service.<name>` module attributes
directly (e.g. `_ensure_topic_thread`, `_send_telegram`, `settings`) and
`stream_agent_turn` must keep calling those SAME functions for the patches to
take effect. `hivepilot.streaming.telegram_channel.TelegramStreamChannel` is
a thin adapter over that exact pipeline, exposed through the protocol below
for introspection/testability -- see that module's docstring.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ThreadRef:
    """A channel-native handle for an agent's per-turn container.

    ``container`` is the channel/chat the thread lives in (a Slack channel
    id, a Telegram chat id, a Discord channel id); ``thread_id`` is the
    thread/topic identifier within it (``thread_ts`` for Slack, a Discord
    thread's own channel id, a Telegram ``message_thread_id``) -- ``None``
    means "no thread, deliver into the container directly" (the threadless
    fallback every channel implementation degrades to rather than ever
    dropping a message).
    """

    channel: str
    container: Any
    thread_id: Any | None = None


@runtime_checkable
class StreamChannel(Protocol):
    """A channel `stream_agent_turn` can deliver a live agent turn to."""

    name: str
    max_len: int

    def enabled(self) -> bool:
        """True when this channel has the credentials + stream target it
        needs configured. A disabled channel is a silent no-op -- never
        raises, never half-configured."""
        ...

    def ensure_agent_thread(self, agent_key: str, title: str) -> ThreadRef | None:
        """Return (creating if absent) the per-agent thread/topic. Best
        effort -- returns ``None`` on any failure rather than raising, so the
        caller falls back to a threadless send instead of dropping content."""
        ...

    def send(self, thread: ThreadRef | None, text: str, *, rich: bool = False) -> None:
        """Deliver *text* (already `format()`-ted for this channel) into
        *thread* (or the channel's default container when ``None``).
        Implementations must degrade (threadless retry, plain-text retry,
        ...) rather than raise for a delivery failure they CAN recover from
        -- a message must never be silently dropped. Raising
        ``NotConfigured`` (see `notification_service.NotConfigured`) is the
        one exception: genuine misconfiguration the caller already checked
        via `enabled()`."""
        ...

    def format(self, markdown: str) -> str:
        """Convert a channel-neutral Markdown string into this channel's
        native markup (Telegram's HTML subset, Slack mrkdwn, Discord
        markdown)."""
        ...


STREAM_CHANNEL_MAP: dict[str, StreamChannel] = {}

# Built-in stream channels, for docs/help/inventory only (mirrors
# KNOWN_NOTIFIER_NAMES) -- NOT enforced at runtime; see StreamChannelRegistry.
KNOWN_STREAM_CHANNEL_NAMES: tuple[str, ...] = ("telegram", "slack", "discord")


class StreamChannelKindCollisionError(RuntimeError):
    pass


class StreamChannelRegistry:
    @staticmethod
    def register(name: str, channel: StreamChannel, *, override: bool = False) -> None:
        if name in STREAM_CHANNEL_MAP and STREAM_CHANNEL_MAP[name] is not channel and not override:
            raise StreamChannelKindCollisionError(
                f"Stream channel '{name}' is already registered to "
                f"{type(STREAM_CHANNEL_MAP[name]).__name__}; refusing to silently replace it"
            )
        STREAM_CHANNEL_MAP[name] = channel

    @staticmethod
    def known_names() -> frozenset[str]:
        return frozenset(STREAM_CHANNEL_MAP)


# ---------------------------------------------------------------------------
# Shared chunk splitter -- generalized from the (now-delegating)
# notification_service._split_for_telegram: `limit` -> `max_len`,
# `html_aware` -> `entity_aware`. Every channel reuses the same
# paragraph/line/word-boundary-aware, never-truncating algorithm. Telegram
# keeps entity-aware splitting (its own <b>/<i>/<code>/<pre>/<a>/<blockquote>
# tags must never straddle a chunk boundary); Slack/Discord split with
# entity_aware=False -- neither channel's native markup needs the same
# balanced-tag bookkeeping Telegram's generated HTML does.
# ---------------------------------------------------------------------------

_TAG_TOKEN_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(?:\s+[^<>]*)?>")
_KNOWN_TAGS = {"b", "i", "code", "pre", "a", "blockquote"}


def find_break(text: str, budget: int) -> int:
    """Return the best index <= *budget* to cut *text* at.

    Prefers a paragraph break (``\\n\\n``), then a line break (``\\n``), then
    a word boundary (space) -- never mid-word if one of those exists within
    the budget. Falls back to a hard cut at *budget* only when none of the
    softer boundaries are available.
    """
    if budget <= 0:
        return 0
    if len(text) <= budget:
        return len(text)
    window = text[:budget]
    idx = window.rfind("\n\n")
    if idx > 0:
        return idx + 2
    idx = window.rfind("\n")
    if idx > 0:
        return idx + 1
    idx = window.rfind(" ")
    if idx > 0:
        return idx + 1
    return budget


def tokenize_tags(text: str) -> list[str]:
    """Split *text* into a flat token list: each known ``<tag>``/``</tag>``
    is its own atomic token (never split), everything else is a plain-text
    token the packer in :func:`split_for` may split further."""
    tokens: list[str] = []
    last = 0
    for m in _TAG_TOKEN_RE.finditer(text):
        if m.group(2) not in _KNOWN_TAGS:
            continue  # not one of ours -- treat as plain text, don't tokenize
        if m.start() > last:
            tokens.append(text[last : m.start()])
        tokens.append(m.group(0))
        last = m.end()
    if last < len(text):
        tokens.append(text[last:])
    return tokens


def split_for(
    text: str,
    max_len: int,
    max_chunks: int = 8,
    *,
    entity_aware: bool = False,
) -> list[str]:
    """Split *text* into ordered, channel-sized chunks -- never truncates.

    Breaks on a paragraph boundary first, then a line boundary, then a word
    boundary (see :func:`find_break`). When *entity_aware* is True, any of
    the known ``<b>``/``<i>``/``<code>``/``<pre>``/``<a>``/``<blockquote>``
    tags that would straddle a chunk boundary are closed at the end of the
    chunk they start in and re-opened at the start of the next one, so every
    returned chunk is independently well-formed HTML.

    Capped at *max_chunks* -- a pathological (multi-hundred-KB) agent dump
    must never turn into dozens of messages; a short note is appended to the
    last chunk when content had to be dropped for the cap. When there is
    more than one chunk, each gets a trailing ``(i/N)`` continuation marker.
    """
    if len(text) <= max_len:
        return [text]

    tokens = tokenize_tags(text) if entity_aware else [text]
    chunks: list[str] = []
    stack: list[str] = []
    idx = 0
    truncated = False

    while idx < len(tokens):
        prefix = "".join(f"<{t}>" for t in stack)
        cur_stack = list(stack)
        current = ""

        while idx < len(tokens):
            tok = tokens[idx]
            tag_match = _TAG_TOKEN_RE.fullmatch(tok) if entity_aware else None

            if tag_match is not None:
                is_close = tag_match.group(1) == "/"
                name = tag_match.group(2)
                trial_stack = list(cur_stack)
                if is_close:
                    if trial_stack and trial_stack[-1] == name:
                        trial_stack.pop()
                else:
                    trial_stack.append(name)
                suffix_len = sum(len(f"</{t}>") for t in reversed(trial_stack))
                if len(prefix) + len(current) + len(tok) + suffix_len > max_len and current:
                    break  # finalize this chunk before the tag
                current += tok
                cur_stack = trial_stack
                idx += 1
                continue

            suffix_len = sum(len(f"</{t}>") for t in reversed(cur_stack))
            budget = max(max_len - len(prefix) - len(current) - suffix_len, 0 if current else 1)
            if budget <= 0:
                break
            if len(tok) <= budget:
                current += tok
                idx += 1
                continue
            cut = find_break(tok, budget)
            if cut <= 0:
                break  # nothing more fits in this chunk
            current += tok[:cut]
            tokens[idx] = tok[cut:]
            break

        suffix = "".join(f"</{t}>" for t in reversed(cur_stack))
        chunks.append(prefix + current + suffix)
        stack = cur_stack

        if len(chunks) >= max_chunks and idx < len(tokens):
            truncated = True
            break

    if truncated:
        remaining = sum(len(t) for t in tokens[idx:])
        chunks[-1] += f"\n\n(… truncated — {remaining} more characters dropped)"

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk}\n\n({i + 1}/{total})" for i, chunk in enumerate(chunks)]

    return chunks


# ---------------------------------------------------------------------------
# Generic per-channel agent-thread store -- Slack/Discord persist their
# agent_key -> thread_id mapping the same registry-shape as Telegram's
# _topics_registry_path/_load_topics/_save_topics, but in a SEPARATE file per
# channel (stream_threads_<channel>.json) so a new channel can never collide
# with -- or migrate/mutate -- Telegram's existing stream_topics.json.
# ---------------------------------------------------------------------------


def default_thread_store_path(channel_name: str) -> Path:
    """Absolute, cwd-independent path for *channel_name*'s agent-thread
    store -- same stable data directory Telegram's own registry uses."""
    from hivepilot.config import settings

    return settings.xdg_data_home / f"stream_threads_{channel_name}.json"


def load_thread_store(path: Path) -> dict[str, Any]:
    """Load an agent_key -> thread_id mapping from disk. Best-effort."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.thread_store.load_failed", path=str(path), error=str(exc))
    return {}


def save_thread_store(path: Path, mapping: dict[str, Any]) -> None:
    """Persist *mapping* to *path*. Best-effort -- never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.thread_store.save_failed", path=str(path), error=str(exc))
