from __future__ import annotations

import html
import json
import os
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

import requests

from hivepilot import outward
from hivepilot.config import settings
from hivepilot.services.config_provenance import mask_id
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Threshold above which a hand-off/stage-output turn is considered "long" —
# below this we keep the exact legacy plain-text rendering (byte-identical,
# single message) since short status/heartbeat turns already read fine.
# Above it we switch to readable HTML formatting (when rich streaming is on)
# and NEVER truncate — see _split_for_telegram below.
_STREAM_MAX_CHARS = 1500

# Telegram's hard message cap is ~4096 chars/bytes. Keep headroom for
# entity close/reopen overhead across a chunk boundary plus the optional
# "(i/N)" continuation marker.
_SPLIT_LIMIT = 3900

# A pathological agent dump (hundreds of KB) must never turn into dozens of
# Telegram messages spamming a topic — cap the number of chunks per turn.
_MAX_CHUNKS = 8

# Human-readable meaning shown next to each live-stream emoji.
_ICON_LABELS = {
    "🚀": "start",
    "🗣": "hand-off",
    "⏸️": "approval needed",
    "💬": "proposal",
    "⚖️": "synthesis",
    "⚔️": "challenge",
    "🛡️": "rebuttal",
    "⚖️ resolved": "resolved",
    "🙋": "needs human",
    "❓": "request",
    "↩️": "answer",
}

# Status badge mapping for the rich HTML card.
_STATUS_BADGES = {
    "PASS": "✅ PASS",
    "BLOCKED": "⛔ BLOCKED",
    "NEEDS_HUMAN": "🙋 NEEDS_HUMAN",
    "ADVISORY": "📋 ADVISORY",
}

# Legacy cwd-relative path for the agent_key -> message_thread_id registry.
# Kept ONLY as a one-time migration source (see _migrate_legacy_topics) --
# this is exactly the bug: a relative path means an OpenRC service (cwd=/)
# and a CLI run (cwd=$HOME) each get their OWN registry, so the second
# context never sees a topic the first already created and duplicates it
# via createForumTopic. The live registry now lives at an absolute,
# cwd-independent path — see _topics_registry_path().
_LEGACY_TOPICS_REGISTRY_PATH = Path(".hivepilot/stream_topics.json")

NOTIFIER_MAP: dict[str, Callable[[str], None]] = {}

# Built-in notifier channels, for docs/help/inventory only (mirrors
# KNOWN_RUNNER_KINDS) — NOT enforced at runtime; see NotifierRegistry.
KNOWN_NOTIFIER_NAMES: tuple[str, ...] = ("slack", "discord", "telegram")


class NotifierKindCollisionError(RuntimeError):
    pass


class NotifierRegistry:
    @staticmethod
    def register(name: str, fn: Callable[[str], None], *, override: bool = False) -> None:
        if name in NOTIFIER_MAP and NOTIFIER_MAP[name] is not fn and not override:
            raise NotifierKindCollisionError(
                f"Notifier '{name}' is already registered to {NOTIFIER_MAP[name].__name__}; "
                f"refusing to silently replace it"
            )
        NOTIFIER_MAP[name] = fn

    @staticmethod
    def known_names() -> frozenset[str]:
        return frozenset(NOTIFIER_MAP)


def send_notification(message: str, channels: Iterable[str] | None = None) -> None:
    # Choke point: `message` frequently embeds `str(exc)` (e.g. a run-failure
    # notification), which can echo a resolved ${secret:NAME} value an agent
    # printed. Redact before it goes out to any outbound channel.
    from hivepilot.services.config_provenance import redact_text

    message = redact_text(message)
    channels = list(channels) if channels else ["slack", "discord", "telegram"]
    # Outward consent (`notify`): a partition dispatched WITHOUT outward
    # consent must not reach Telegram/Slack/Discord/Signal. Checked here --
    # the one fan-out choke point every plain notification passes through --
    # rather than at each of the 30+ call sites. `permits` logs the
    # suppression; a silently dropped notification would be
    # indistinguishable from a broken notifier.
    if not outward.permits(
        "notify",
        surface="notification_service.send_notification",
        detail=",".join(str(channel) for channel in channels),
    ):
        return
    for channel in channels:
        channel = channel.lower()
        fn = NOTIFIER_MAP.get(channel)
        if fn is None:
            logger.warning("notification.unknown_channel", channel=channel)
            continue
        try:
            fn(message)
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
    # Outward consent (`notify`): an event webhook is an outbound POST to a
    # third party. Same axis, same gate.
    if not outward.permits("notify", surface="notification_service.emit_event", detail=event):
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


# Public alias — the same class, importable by plugin notifiers as
# `from hivepilot.services.notification_service import NotConfigured`.
NotConfigured = _NotConfigured


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


class TelegramSendError(RuntimeError):
    """A Telegram `sendMessage` call returned a non-2xx response.

    Carries the fields a human needs to diagnose a Telegram failure WITHOUT
    reproducing it (explicit-failure-logs sprint, Part A.3): the HTTP status
    code and Telegram's own ``description`` (e.g. "Can't parse entities:
    can't find end of the entity ...") parsed straight out of the JSON error
    body -- `requests.Response.raise_for_status()` alone only ever produces a
    generic "400 Client Error: Bad Request for url: ..." with NONE of that
    detail. `chat_id`/`message_thread_id` are the raw (unmasked) values the
    call was made with -- callers must mask `chat_id` themselves (see
    `hivepilot.services.config_provenance.mask_id`) before logging it.
    """

    def __init__(
        self,
        *,
        status_code: int,
        description: str | None,
        chat_id: int | str | None,
        message_thread_id: int | None,
    ) -> None:
        super().__init__(f"Telegram sendMessage failed: {status_code} {description or ''}".strip())
        self.status_code = status_code
        self.description = description
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id


def _send_telegram(
    message: str,
    chat_id: int | str | None = None,
    message_thread_id: int | None = None,
    parse_mode: str | None = None,
) -> None:
    token = settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = (
        chat_id or settings.telegram_notification_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    )
    if not chat_id and settings.telegram_allowed_chat_ids:
        chat_id = settings.telegram_allowed_chat_ids[0]
    if not token or not chat_id:
        raise _NotConfigured("Telegram not configured (token or notification chat_id missing)")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": message}
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    resp = requests.post(url, json=payload, timeout=5)
    # Telegram returns HTTP 400 for a malformed HTML request (e.g. an
    # unbalanced/unsupported entity our formatter didn't anticipate). Raise a
    # `TelegramSendError` (carrying status + Telegram's own `description`)
    # rather than the generic `resp.raise_for_status()` so callers like
    # `_send_chunks` can retry that one chunk as plain text AND log a real
    # reason instead of the failure silently vanishing into "400 Client
    # Error: Bad Request for url: ...".
    if not resp.ok:
        description: str | None = None
        try:
            body = resp.json()
            if isinstance(body, dict):
                description = body.get("description")
        except ValueError:
            pass
        raise TelegramSendError(
            status_code=resp.status_code,
            description=description,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
        )


NotifierRegistry.register("slack", _send_slack)
NotifierRegistry.register("discord", _send_discord)
NotifierRegistry.register("telegram", _send_telegram)


# ---------------------------------------------------------------------------
# Stale/closed forum-topic detection + self-heal
#
# A `message_thread_id` cached in the topics registry can go dead: an
# operator deletes the topic (or a registry gets migrated from another
# context — see `_migrate_legacy_topics`). Telegram then rejects EVERY send
# that carries it with a 400 whose `description` names the problem. Without
# this detection, the existing per-chunk "retry as plain text" fallback
# retries with the SAME dead thread id and fails identically — the message
# is lost and the dead id stays cached forever (see `_send_one_chunk`, which
# is what actually invalidates + recreates).
#
# A *closed* (not deleted) topic is a DIFFERENT error class
# (e.g. "TOPIC_CLOSED") and must NOT be treated the same way: the operator
# closed it deliberately, so recreating it would defeat that decision —
# route to the chat's General topic instead (see `_send_one_chunk`).
# ---------------------------------------------------------------------------

_STALE_TOPIC_MARKERS: tuple[str, ...] = (
    "message thread not found",
    "topic_deleted",
    "thread not found",
)
_CLOSED_TOPIC_MARKERS: tuple[str, ...] = ("topic_closed",)


def _description_matches(description: str | None, markers: tuple[str, ...]) -> bool:
    if not description:
        return False
    low = description.lower()
    return any(marker in low for marker in markers)


def _is_stale_topic_error(description: str | None) -> bool:
    """True when *description* names a deleted/missing forum topic."""
    return _description_matches(description, _STALE_TOPIC_MARKERS)


def _is_closed_topic_error(description: str | None) -> bool:
    """True when *description* names a CLOSED (not deleted) forum topic."""
    return _description_matches(description, _CLOSED_TOPIC_MARKERS)


def _topics_registry_path() -> Path:
    """Resolve the absolute, cwd-INDEPENDENT path for the topics registry.

    Uses the `stream_topics_registry_path` override when configured,
    otherwise defaults to `xdg_data_home/stream_topics.json` — the same
    stable data directory already used for plugins/config-repo (see
    `Settings.xdg_data_home`). Reusing it (instead of a cwd-relative default,
    or `base_dir`, which is itself frozen to `Path.cwd()` at import time and
    would reproduce the exact same split) is what makes every process share
    ONE registry regardless of its working directory.
    """
    override = settings.stream_topics_registry_path
    if override is not None:
        return settings.resolve_path(Path(override))
    return settings.xdg_data_home / "stream_topics.json"


def _migrate_legacy_topics(new_path: Path) -> dict[str, int]:
    """One-time migration: legacy cwd-relative registry -> new absolute path.

    Only runs when *new_path* doesn't exist yet, so an existing deployment
    keeps its already-created topics instead of losing them (and creating a
    THIRD set) the first time it runs with the fixed, absolute path.
    """
    legacy = _LEGACY_TOPICS_REGISTRY_PATH
    if not legacy.exists():
        return {}
    try:
        data: dict[str, int] = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.topics.migration_read_failed", error=str(exc))
        return {}
    logger.warning(
        "stream.topics.migrated_legacy_registry",
        legacy_path=str(legacy.resolve()),
        new_path=str(new_path),
        count=len(data),
    )
    _write_topics(new_path, data)
    return data


def _load_topics() -> dict[str, int]:
    """Load the agent_key -> message_thread_id registry from disk. Best-effort."""
    try:
        path = _topics_registry_path()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return _migrate_legacy_topics(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.topics.load_failed", error=str(exc))
    return {}


def _write_topics(path: Path, mapping: dict[str, int]) -> None:
    """Persist *mapping* to *path*. Best-effort — never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.topics.save_failed", error=str(exc))


def _save_topics(mapping: dict[str, int]) -> None:
    """Persist the topics registry to disk. Best-effort."""
    _write_topics(_topics_registry_path(), mapping)


def _invalidate_topic(agent_key: str) -> int | None:
    """Remove *agent_key* from the topics registry and persist. Best-effort.

    Returns the removed (dead) thread id, or None if the key wasn't cached.
    This is the self-heal step: without it, the dead id would be re-read by
    the next `_ensure_topic_thread` call and every future send would fail
    with the same "thread not found" error forever.
    """
    registry = _load_topics()
    dead = registry.pop(agent_key, None)
    if dead is not None:
        _save_topics(registry)
    return dead


def _normalize(text: str) -> str:
    """Lowercase + strip accents for fuzzy matching."""
    return unicodedata.normalize("NFD", text.lower()).encode("ascii", "ignore").decode()


def _resolve_agent_key(actor: str) -> str:
    """Map an actor display string (e.g. 'Blaise (CTO)') to a stable role key.

    Matches against ROLES display_name (accent/case-insensitive). Falls back to
    a slug derived from the actor string. Never returns an empty string.
    """
    from hivepilot.roles import ROLES

    actor_norm = _normalize(actor)
    for key, role in ROLES.items():
        if role.display_name and _normalize(role.display_name) in actor_norm:
            return key
        if _normalize(role.title) in actor_norm:
            return key
    # Fallback: slug from first word of actor
    slug = actor_norm.split()[0] if actor_norm.strip() else "general"
    return slug or "general"


def _ensure_topic_thread(agent_key: str, title: str) -> int | None:
    """Return the message_thread_id for *agent_key*, creating it if absent.

    Calls Telegram createForumTopic when the key is not in the registry.
    Best-effort: any failure returns None (never raises).
    """
    token = settings.telegram_bot_token
    chat_id = settings.telegram_stream_chat_id
    if not token or not chat_id:
        return None

    registry = _load_topics()
    if agent_key in registry:
        return registry[agent_key]

    try:
        url = f"https://api.telegram.org/bot{token}/createForumTopic"
        resp = requests.post(url, json={"chat_id": chat_id, "name": title}, timeout=5)
        data = resp.json()
        if data.get("ok"):
            thread_id: int = data["result"]["message_thread_id"]
            registry[agent_key] = thread_id
            _save_topics(registry)
            return thread_id
        logger.warning("stream.topics.create_failed", agent_key=agent_key, response=data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.topics.create_error", agent_key=agent_key, error=str(exc))
    return None


def _render_rich_card(
    *,
    icon: str,
    actor: str,
    target: str | None,
    report: Any,  # AgentReport
) -> str:
    """Render an HTML card for Telegram's HTML parse mode.

    Returns a string ready for ``parse_mode="HTML"``. All user-derived text
    is escaped via ``html.escape``. The card is never truncated here — a
    card longer than Telegram's message cap is split into multiple ordered
    messages by :func:`_split_for_telegram` at the call site.
    """
    lines: list[str] = []

    # Header: icon <b>Actor</b> → <i>Target</i>
    header = f"{icon} <b>{html.escape(actor)}</b>"
    if target:
        header += f" → <i>{html.escape(target)}</i>"
    lines.append(header)

    # Status badge
    if report.status:
        badge = _STATUS_BADGES.get(report.status.upper(), f"📋 {html.escape(report.status)}")
        lines.append(badge)

    # Summary bullets — max 5 items shown inline (a deliberate curation
    # choice, not a character-length truncation); each bullet is rendered in
    # FULL, never clipped. If there are more than 5, a count-based notice
    # (with a link to the full artifact when available) explains the rest —
    # the content itself is never cut off mid-sentence.
    from hivepilot.services.agent_report import to_telegram_text

    # Find vault artifact link (any .md path in report.links)
    artifact_link: str | None = next(
        (lnk for lnk in report.links if lnk.endswith(".md") and not lnk.startswith("http")),
        None,
    )

    shown = report.summary[:5]
    bullet_lines = [
        f"• {html.escape(clean)}" for bullet in shown if (clean := to_telegram_text(bullet).strip())
    ]

    if len(report.summary) > len(shown):
        more = len(report.summary) - len(shown)
        notice = f"… (+{more} more — full details in the vault artifact)"
        if artifact_link:
            safe = html.escape(artifact_link)
            notice += f' <a href="file://{safe}">{safe}</a>'
        bullet_lines.append(notice)

    lines.extend(bullet_lines)

    # Next handoff
    if report.next_handoff:
        lines.append(f"↪ next: {html.escape(report.next_handoff)}")

    # Confidence
    if report.confidence:
        lines.append(f"confidence: {html.escape(report.confidence)}")

    # Links (as <a> tags)
    for link in report.links:
        safe = html.escape(link)
        if link.startswith("http"):
            lines.append(f'<a href="{safe}">{safe}</a>')
        else:
            lines.append(f'<a href="file://{safe}">{safe}</a>')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram-safe Markdown -> HTML subset + entity-aware chunk splitting
#
# Together these two pieces replace the old hard truncation (a message just
# got cut at ~1500/~3500 chars with a trailing "…") with: format a small,
# safe HTML subset for readability, then split into as many ordered
# Telegram-sized messages as needed — never dropping content (up to the
# sane _MAX_CHUNKS safety cap for pathological outputs).
# ---------------------------------------------------------------------------

# Fenced code block: ```lang\n...\n``` (language hint optional, newline
# after the opening fence optional so a single-line ```x``` also matches).
# Runs on RAW (pre-escape) text — content is escaped when it's finally
# wrapped in <pre>, everything else in the block-level pass below.
_FENCE_RE = re.compile(r"```[ \t]*[\w+-]*\r?\n?(.*?)```", re.DOTALL)
# Block-level, line-anchored constructs (matched one line at a time against
# the fence-stashed text, so nothing inside a code block can trigger these).
_HEADER_LINE_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*$")
_HRULE_LINE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,}|={3,})$")
_BLOCKQUOTE_LINE_RE = re.compile(r"^>[ \t]?(.*)$")
_BULLET_LINE_RE = re.compile(r"^([ \t]*)[-*][ \t]+(.+)$")
_NUMBERED_LINE_RE = re.compile(r"^([ \t]*)(\d+)\.[ \t]+(.+)$")
_TABLE_ROW_LINE_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_SEP_LINE_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$"
)

# Inline (within-line) constructs, applied to a single line/cell's content.
# Image syntax: keep the alt text, drop the "![" "](url)" wrapper — run on
# RAW text before escaping (its brackets/parens are markdown, not HTML).
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
# Inline code: `x` (no embedded backtick or newline).
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# Bold: **x** or __x__ (checked first so a lone remaining '*'/'_' pair is
# unambiguously italic).
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
# Italic: *x* or _x_ (single marker — only what BOLD_RE left behind).
_ITALIC_RE = re.compile(r"\*(.+?)\*|_(.+?)_", re.DOTALL)

# Placeholder used to protect already-rendered spans (fences, tables) from
# being re-processed by a later pass.
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")

# Any HTML open/close tag we might emit ourselves — used to tokenize a
# rendered string for entity-aware splitting. Only ever applied to text WE
# built (post html.escape), so a literal "<" from agent output is already
# "&lt;" by the time this regex runs and can never masquerade as a tag. Kept
# here (duplicated, not imported) alongside the identical constant in
# `hivepilot.streaming.base` -- some existing tests reference
# `notification_service._TAG_TOKEN_RE` directly, and importing it from
# `hivepilot.streaming.base` at this module's top level would import the
# `hivepilot.streaming` package (which imports `telegram_channel`, which
# imports THIS module) before it has finished loading.
_TAG_TOKEN_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(?:\s+[^<>]*)?>")

# Tags our formatter can emit — anything else in the stream is plain text.
_KNOWN_TAGS = {"b", "i", "code", "pre", "a", "blockquote"}


def _render_table(rows: list[str]) -> str:
    """Render markdown table *rows* (pipe-delimited, separator row already
    excluded by the caller) as a column-aligned monospace block.

    Telegram HTML has no table support — a raw ``| a | b |`` dump reads as
    a broken wall of pipes, so instead we pad each column to its widest
    cell and wrap the whole thing in ``<pre>`` for a readable, aligned
    report block.
    """
    parsed = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
    ncols = max((len(r) for r in parsed), default=0)
    parsed = [r + [""] * (ncols - len(r)) for r in parsed]
    widths = [max((len(r[c]) for r in parsed), default=0) for c in range(ncols)]
    lines = ["  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)) for row in parsed]
    return f"<pre>{html.escape(chr(10).join(lines))}</pre>"


def _format_for_telegram_html(text: str) -> str:
    """Render agent output as a clean, readable Telegram HTML report.

    Telegram's HTML parse mode only supports a small tag set (``<b>``,
    ``<i>``, ``<u>``, ``<s>``, ``<code>``, ``<pre>``, ``<a>``,
    ``<blockquote>``, ``<tg-spoiler>``) — there are no real headers or
    tables, so this is a deliberate, thoughtful subset conversion rather
    than a literal 1:1 Markdown mirror:

    - ``#``/``##``/``###`` headers -> ``<b>`` on its own (blank-line
      separated) line, sub-headers (level >= 2) get a "▸ " marker so
      nesting is visually obvious without real heading levels.
    - ``**bold**``/``__bold__`` -> ``<b>``, ``*italic*``/``_italic_`` -> ``<i>``.
    - Inline `` `code` `` -> ``<code>``; fenced ```` ``` ```` blocks -> ``<pre>``
      (indentation/newlines preserved verbatim).
    - ``- ``/``* `` bullets -> ``• `` with a 2-space indent per nesting
      level; ``1. `` numbered items keep their number.
    - ``> `` blockquotes -> ``<blockquote>``.
    - Markdown tables -> a column-aligned monospace block inside ``<pre>``
      (Telegram can't render real tables).
    - ``---``/``***`` horizontal rules -> a thin "──────────" separator.
    - ``![alt](url)`` images -> just the alt text (there's nowhere to put
      an image in a chat message).
    - 3+ blank lines collapse to one; trailing whitespace is trimmed.

    Everything is HTML-escaped (``&``, ``<``, ``>``) BEFORE a tag is ever
    added, and unbalanced markdown (a stray ``**`` or backtick) is simply
    left as literal escaped text rather than a half-applied tag — malformed
    input can never produce a dangling entity that gets the whole message
    rejected by Telegram with a 400.
    """
    stash: list[str] = []

    def _put(rendered: str) -> str:
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    def _inline(raw: str) -> str:
        """Apply image/code/bold/italic to one line's (or table cell's)
        raw markdown text, escaping first so nothing agent-controlled can
        inject markup."""
        raw = _IMAGE_RE.sub(lambda m: m.group(1), raw)
        escaped = html.escape(raw)
        escaped = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
        escaped = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", escaped)
        escaped = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", escaped)
        return escaped

    # Fenced code blocks are extracted first, from the RAW text, so nothing
    # below (headers, tables, inline formatting) ever reaches into one.
    staged = _FENCE_RE.sub(lambda m: _put(f"<pre>{html.escape(m.group(1))}</pre>"), text)

    lines = staged.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if _TABLE_ROW_LINE_RE.match(line):
            block = [line]
            i += 1
            while i < len(lines) and _TABLE_ROW_LINE_RE.match(lines[i]):
                block.append(lines[i])
                i += 1
            if len(block) > 1 and _TABLE_SEP_LINE_RE.match(block[1]):
                block.pop(1)
            out.append(_put(_render_table(block)))
            continue

        if _HRULE_LINE_RE.match(line.strip()):
            out.append("──────────")
            i += 1
            continue

        m = _HEADER_LINE_RE.match(line)
        if m:
            level = len(m.group(1))
            marker = "▸ " if level >= 2 else ""
            if out and out[-1] != "":
                out.append("")  # blank line before a header so it stands out
            out.append(f"<b>{marker}{_inline(m.group(2))}</b>")
            i += 1
            continue

        m = _BLOCKQUOTE_LINE_RE.match(line)
        if m:
            out.append(f"<blockquote>{_inline(m.group(1))}</blockquote>")
            i += 1
            continue

        m = _BULLET_LINE_RE.match(line)
        if m:
            indent, content = m.group(1), m.group(2)
            level = len(indent.replace("\t", "  ")) // 2
            out.append(f"{'  ' * level}• {_inline(content)}")
            i += 1
            continue

        m = _NUMBERED_LINE_RE.match(line)
        if m:
            indent, num, content = m.group(1), m.group(2), m.group(3)
            level = len(indent.replace("\t", "  ")) // 2
            out.append(f"{'  ' * level}{num}. {_inline(content)}")
            i += 1
            continue

        out.append(_inline(line))
        i += 1

    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)  # collapse 3+ blank lines
    result = "\n".join(line.rstrip() for line in result.split("\n")).strip()

    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], result)


_STRIP_HTML_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Best-effort plain-text degrade for an HTML chunk Telegram rejected.

    Used as the per-chunk fallback when a formatted ``sendMessage`` 400s
    (usually a parse-entity edge case our subset converter didn't fully
    anticipate) — strips our tags and unescapes entities so the CONTENT
    still reaches the operator even if the formatting is lost.
    """
    return html.unescape(_STRIP_HTML_RE.sub("", text))


def _split_for_telegram(
    text: str,
    limit: int = _SPLIT_LIMIT,
    max_chunks: int = _MAX_CHUNKS,
    *,
    html_aware: bool = True,
) -> list[str]:
    """Split *text* into ordered Telegram-sized chunks — never truncates.

    Thin delegation to the channel-agnostic
    :func:`hivepilot.streaming.base.split_for` (``limit`` -> ``max_len``,
    ``html_aware`` -> ``entity_aware``) — this function's own name/signature/
    behavior are kept byte-identical (same defaults, same output) for every
    existing caller and test; only the algorithm's *body* moved so Slack/
    Discord can reuse the exact same paragraph/line/word-boundary-aware,
    never-truncating splitter (see multi-channel-streaming sprint). Breaks on
    a paragraph boundary first, then a line boundary, then a word boundary.
    When *html_aware* (the default — pass ``False`` for genuinely plain,
    non-HTML text so literal ``<``/``>`` in agent output, e.g. ``List<int>``,
    is never mistaken for a tag), any of our own
    ``<b>``/``<i>``/``<code>``/``<pre>``/``<a>``/``<blockquote>`` tags that
    would straddle a chunk boundary are closed at the end of the chunk they
    start in and re-opened at the start of the next one, so every returned
    chunk is independently well-formed HTML. Capped at *max_chunks*; when
    there is more than one chunk, each gets a trailing ``(i/N)`` marker.
    """
    from hivepilot.streaming.base import split_for

    return split_for(text, limit, max_chunks, entity_aware=html_aware)


def _deliver_threadless(
    chunk: str,
    *,
    chat_id: Any,
    parse_mode: str | None,
    agent_key: str | None,
) -> None:
    """Last-resort send with NO ``message_thread_id`` (the chat's General
    topic) — used once a topic is confirmed dead/closed and recreation
    either isn't attempted (closed) or failed (dead, recreate/resend both
    failed). Tries the original *parse_mode* first so formatting survives
    when the only problem was the topic, then degrades to plain text.
    Content must always reach the operator — this is the final line of
    defense against ever silently dropping a message.
    """
    try:
        _send_telegram(chunk, chat_id=chat_id, message_thread_id=None, parse_mode=parse_mode)
    except _NotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        plain = _strip_html(chunk) if parse_mode else chunk
        _send_telegram(plain, chat_id=chat_id, message_thread_id=None, parse_mode=None)
        logger.info(
            "stream.topic_self_heal_delivered_general_plain",
            agent_key=agent_key,
            chat_id=mask_id(chat_id),
            error=str(exc),
        )
        return
    logger.info(
        "stream.topic_self_heal_delivered_general",
        agent_key=agent_key,
        chat_id=mask_id(chat_id),
        parse_mode=parse_mode,
    )


def _send_one_chunk(
    chunk: str,
    *,
    chat_id: Any,
    message_thread_id: int | None,
    parse_mode: str | None,
    agent_key: str | None,
    topic_title: str | None,
) -> int | None:
    """Send a single *chunk*, self-healing a dead/closed registry topic id
    and never losing the message. Returns the ``message_thread_id`` that
    subsequent chunks of the same turn should use (unchanged unless this
    chunk triggered a stale-topic recreate, in which case later chunks reuse
    the fresh id instead of repeating the invalidate/recreate dance).
    """
    try:
        _send_telegram(
            chunk, chat_id=chat_id, message_thread_id=message_thread_id, parse_mode=parse_mode
        )
        return message_thread_id
    except _NotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        description = getattr(exc, "description", None)

        if (
            message_thread_id is not None
            and agent_key is not None
            and _is_closed_topic_error(description)
        ):
            # Operator closed this topic deliberately -- do NOT recreate it,
            # route to General instead.
            logger.warning(
                "stream.topic_closed_fallback_general",
                agent_key=agent_key,
                chat_id=mask_id(chat_id),
                message_thread_id=message_thread_id,
                description=description,
            )
            _deliver_threadless(chunk, chat_id=chat_id, parse_mode=parse_mode, agent_key=agent_key)
            return None

        if (
            message_thread_id is not None
            and agent_key is not None
            and _is_stale_topic_error(description)
        ):
            dead = _invalidate_topic(agent_key)
            logger.warning(
                "stream.topic_stale_invalidated",
                agent_key=agent_key,
                chat_id=mask_id(chat_id),
                dead_message_thread_id=dead if dead is not None else message_thread_id,
                description=description,
            )
            new_thread_id = _ensure_topic_thread(agent_key, topic_title or agent_key)
            if new_thread_id is not None:
                logger.info(
                    "stream.topic_recreated",
                    agent_key=agent_key,
                    chat_id=mask_id(chat_id),
                    message_thread_id=new_thread_id,
                )
                try:
                    _send_telegram(
                        chunk,
                        chat_id=chat_id,
                        message_thread_id=new_thread_id,
                        parse_mode=parse_mode,
                    )
                    return new_thread_id
                except Exception as retry_exc:  # noqa: BLE001
                    logger.warning(
                        "stream.topic_recreate_resend_failed",
                        agent_key=agent_key,
                        chat_id=mask_id(chat_id),
                        message_thread_id=new_thread_id,
                        error=str(retry_exc),
                    )
            # Recreate failed, or the resend to the fresh topic also failed
            # -- never drop the message, deliver it threadless instead.
            _deliver_threadless(chunk, chat_id=chat_id, parse_mode=parse_mode, agent_key=agent_key)
            return None

        # Not a topic problem -- keep the existing "retry as plain text"
        # degrade for a parse-entity edge case (e.g. bad markdown); registry
        # untouched. Explicit-failure-logs sprint, Part A.3: a
        # `TelegramSendError` carries the real HTTP status + Telegram's own
        # `description` ("Can't parse entities: ..."); any OTHER exception
        # (a network error, ...) degrades to just `error=str(exc)` -- either
        # way a 400 is never just a silent retry with no diagnostic trail.
        if parse_mode is None:
            raise
        logger.warning(
            "stream.chunk_send_failed_retry_plain",
            kind="stream_chunk",
            status_code=getattr(exc, "status_code", None),
            description=description,
            chat_id=mask_id(chat_id),
            message_thread_id=message_thread_id,
            error=str(exc),
        )
        try:
            _send_telegram(
                _strip_html(chunk),
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                parse_mode=None,
            )
        except Exception as fallback_exc:  # noqa: BLE001
            logger.error(
                "stream.chunk_send_fallback_failed",
                kind="stream_chunk",
                chat_id=mask_id(chat_id),
                message_thread_id=message_thread_id,
                error=str(fallback_exc),
            )
            raise
        logger.info(
            "stream.chunk_send_fallback_succeeded",
            kind="stream_chunk",
            chat_id=mask_id(chat_id),
            message_thread_id=message_thread_id,
        )
        return message_thread_id


def _send_chunks(
    text: str,
    *,
    chat_id: Any,
    message_thread_id: int | None,
    parse_mode: str | None,
    html_aware: bool,
    agent_key: str | None = None,
    topic_title: str | None = None,
) -> None:
    """Split *text* and send each chunk, in order, to the same chat + topic.

    If a formatted (``parse_mode="HTML"``) chunk is rejected by Telegram
    (a 400 — typically a parse-entity edge case), that ONE chunk is retried
    as plain text so the content always reaches the operator even when
    formatting doesn't. ``_NotConfigured`` always propagates unchanged so
    the caller's existing best-effort no-op behaviour is preserved.

    When *agent_key* is given (the registry key `message_thread_id` came
    from), a "message thread not found"/"TOPIC_DELETED" class of error
    self-heals: the dead registry entry is invalidated, a fresh topic is
    created via `_ensure_topic_thread`, and every remaining chunk (including
    the one that failed) is sent to the NEW thread id — see
    `_send_one_chunk`. A "TOPIC_CLOSED" error instead routes to the chat's
    General topic without recreating (the operator closed it on purpose).
    """
    thread_id = message_thread_id
    for chunk in _split_for_telegram(text, html_aware=html_aware):
        thread_id = _send_one_chunk(
            chunk,
            chat_id=chat_id,
            message_thread_id=thread_id,
            parse_mode=parse_mode,
            agent_key=agent_key,
            topic_title=topic_title,
        )


def _stream_agent_turn_telegram(
    *,
    actor: str,
    stage: str | None = None,
    target: str | None = None,
    summary: str | None = None,
    icon: str = "🗣",
) -> None:
    """Live-stream a single agent's turn to Telegram (outbound ``sendMessage`` only).

    This is Telegram's ORIGINAL, byte-identical rendering pipeline (rich HTML
    cards, entity-aware chunk splitting, stale/closed-topic self-heal) --
    unchanged since before the multi-channel-streaming sprint. `stream_agent_turn`
    (below) is the public, channel-agnostic entry point: it calls this function
    for Telegram specifically (so every existing Telegram test, many of which
    patch this module's own attributes directly, keeps passing unmodified) and
    separately fans out to any other ENABLED `StreamChannel` (Slack, Discord, ...).

    A silent no-op when streaming is disabled or Telegram is unconfigured — it
    must never break a run.

    When ``settings.telegram_stream_rich`` is True and the summary contains
    structured content (status badge or bullet points), renders an HTML card
    instead of plain text. A LONG unstructured summary (one that would have
    been clipped under the old ``_STREAM_MAX_CHARS`` cutoff) is rendered
    with the same safe HTML formatting instead of a bare plain snippet.
    Short status/heartbeat turns keep the exact legacy plain rendering.

    Content is NEVER truncated: anything over Telegram's message cap is
    split into multiple ordered messages (see :func:`_split_for_telegram`)
    sent to the same chat and topic (``message_thread_id``), instead of
    being cut off with a trailing "…".
    """
    if not settings.telegram_stream_live:
        return

    # Choke point: `summary` is a stage/turn's agent output and can echo a
    # resolved ${secret:NAME} value. Redact before it flows into either the
    # rich HTML card or the plain-text fallback below — same leak class as
    # send_notification, same one-line fix.
    from hivepilot.services.config_provenance import redact_text

    summary = redact_text(summary) if summary is not None else summary

    message_thread_id: int | None = None
    stream_agent_key: str | None = None
    stream_topic_title: str | None = None
    if settings.telegram_stream_topics and settings.telegram_stream_chat_id:
        stream_agent_key = _resolve_agent_key(actor)
        stream_topic_title = f"{actor}"
        message_thread_id = _ensure_topic_thread(stream_agent_key, stream_topic_title)

    use_rich = getattr(settings, "telegram_stream_rich", True)
    chat_id = settings.telegram_stream_chat_id
    label = _ICON_LABELS.get(icon)
    tag = f"{icon} ({label})" if label else icon

    message_text: str | None = None
    parse_mode: str | None = None
    html_aware = False

    if use_rich and summary:
        try:
            from hivepilot.services.agent_report import parse_agent_report

            report = parse_agent_report(summary)
            has_structure = bool(report.status or report.summary)
            if has_structure:
                message_text = _render_rich_card(
                    icon=icon,
                    actor=actor,
                    target=target,
                    report=report,
                )
                parse_mode = "HTML"
                html_aware = True
            elif len(summary) > _STREAM_MAX_CHARS:
                # Long, unstructured hand-off/stage output — the case that
                # used to get clipped at _STREAM_MAX_CHARS. Render readable
                # HTML instead of a plain, collapsed-whitespace snippet.
                header_html = f"<b>{html.escape(tag)} {html.escape(actor)}</b>"
                if stage:
                    header_html += f" — {html.escape(stage)}"
                body_lines = [header_html]
                if target:
                    body_lines.append(f"   ↳ {html.escape(target)}")
                body_lines.append(_format_for_telegram_html(summary))
                message_text = "\n".join(body_lines)
                parse_mode = "HTML"
                html_aware = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream.rich_render_failed", error=str(exc))
            message_text = None
            parse_mode = None
            html_aware = False
            # Fall through to plain text

    # --- Plain-text fallback (rich disabled, unstructured+short, or the
    # rich render above raised) — same legacy rendering as before, minus the
    # hard truncation: full content, split into multiple messages if long. ---
    if message_text is None:
        header = f"{tag} {actor}" + (f" — {stage}" if stage else "")
        plain_lines = [header]
        if target:
            plain_lines.append(f"   ↳ {target}")
        if summary:
            snippet = " ".join(summary.split())
            if snippet:
                plain_lines.append(f"   {snippet}")
        message_text = "\n".join(plain_lines)
        parse_mode = None
        html_aware = False

    try:
        # Live agent stream goes to its dedicated channel when set, else
        # falls back to the main notification chat. Never truncated: split
        # into as many ordered messages as needed (same chat + topic).
        _send_chunks(
            message_text,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            parse_mode=parse_mode,
            html_aware=html_aware,
            agent_key=stream_agent_key,
            topic_title=stream_topic_title,
        )
    except _NotConfigured:
        pass  # Telegram not set up — streaming is best-effort
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.failed", error=str(exc))


def _render_markdown_turn(
    *,
    icon: str,
    actor: str,
    stage: str | None,
    target: str | None,
    summary: str | None,
) -> str:
    """Render a channel-NEUTRAL Markdown turn card for the generic
    `StreamChannel` path (Slack, Discord, ...) -- reuses the same
    structured-report detection Telegram's own rich card uses
    (`parse_agent_report`) but emits plain Markdown instead of Telegram's
    HTML subset; each channel's own `format()` converts it to its native
    dialect (mrkdwn, Discord markdown, ...).
    """
    label = _ICON_LABELS.get(icon)
    tag = f"{icon} ({label})" if label else icon
    lines = [f"**{tag} {actor}**" + (f" — {stage}" if stage else "")]
    if target:
        lines.append(f"   ↳ {target}")
    if not summary:
        return "\n".join(lines)

    report = None
    has_structure = False
    try:
        from hivepilot.services.agent_report import parse_agent_report

        report = parse_agent_report(summary)
        has_structure = bool(report.status or report.summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.generic_render_failed", error=str(exc))

    if has_structure and report is not None:
        from hivepilot.services.agent_report import to_telegram_text

        if report.status:
            lines.append(_STATUS_BADGES.get(report.status.upper(), report.status))
        shown = report.summary[:5]
        for bullet in shown:
            clean = to_telegram_text(bullet).strip()
            if clean:
                lines.append(f"- {clean}")
        if len(report.summary) > len(shown):
            lines.append(f"… (+{len(report.summary) - len(shown)} more)")
        if report.next_handoff:
            lines.append(f"↪ next: {report.next_handoff}")
        if report.confidence:
            lines.append(f"confidence: {report.confidence}")
        lines.extend(report.links)
    else:
        lines.append(summary)

    return "\n".join(lines)


def _stream_agent_turn_generic(
    channel: Any,
    *,
    actor: str,
    stage: str | None,
    target: str | None,
    summary: str | None,
    icon: str,
) -> None:
    """Deliver a turn to a non-Telegram `StreamChannel` (Slack, Discord, a
    plugin-registered channel, ...) via the generic protocol: render a
    channel-neutral Markdown card, `format()` it into the channel's native
    markup, `ensure_agent_thread()` the per-agent thread, split for the
    channel's `max_len` (see `hivepilot.streaming.base.split_for`), and
    `send()` every chunk in order.
    """
    from hivepilot.streaming.base import split_for

    markdown = _render_markdown_turn(
        icon=icon, actor=actor, stage=stage, target=target, summary=summary
    )
    formatted = channel.format(markdown)
    agent_key = _resolve_agent_key(actor)
    thread = channel.ensure_agent_thread(agent_key, actor)
    for chunk in split_for(formatted, channel.max_len, entity_aware=False):
        channel.send(thread, chunk, rich=True)


def stream_agent_turn(
    *,
    actor: str,
    stage: str | None = None,
    target: str | None = None,
    summary: str | None = None,
    icon: str = "🗣",
) -> None:
    """Live-stream a single agent's turn to every ENABLED stream channel --
    Telegram, Slack, Discord, or a plugin-registered channel (see
    `hivepilot.streaming`).

    Channel-agnostic fan-out: each channel delivers independently,
    best-effort -- a failure in one channel never blocks the others, and no
    channel configured at all is a silent no-op. A deployment with only the
    Telegram settings configured behaves EXACTLY as before this sprint --
    Telegram keeps its own dedicated, byte-identical rendering pipeline (see
    `_stream_agent_turn_telegram`); Slack/Discord/plugins render via the
    generic `StreamChannel` protocol (`_stream_agent_turn_generic`).
    """
    from hivepilot.services.config_provenance import redact_text

    # Outward consent (`notify`): every `stream_*` helper below funnels
    # through this function, so one gate covers the whole live-stream
    # surface (Telegram's dedicated path AND the generic StreamChannel
    # fan-out).
    if not outward.permits(
        "notify", surface="notification_service.stream_agent_turn", detail=actor
    ):
        return

    summary = redact_text(summary) if summary is not None else summary

    try:
        _stream_agent_turn_telegram(
            actor=actor, stage=stage, target=target, summary=summary, icon=icon
        )
    except Exception as exc:  # noqa: BLE001 -- extra safety net; the callee already never raises
        logger.warning("stream.telegram_failed", error=str(exc))

    from hivepilot.streaming.base import STREAM_CHANNEL_MAP

    for name, channel in STREAM_CHANNEL_MAP.items():
        if name == "telegram":
            continue  # delivered above via the dedicated, byte-identical path
        try:
            if not channel.enabled():
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream.enabled_check_failed", channel=name, error=str(exc))
            continue
        try:
            _stream_agent_turn_generic(
                channel, actor=actor, stage=stage, target=target, summary=summary, icon=icon
            )
        except _NotConfigured:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream.channel_failed", channel=name, error=str(exc))


def stream_challenge(actor: str, target: str, point: str) -> None:
    """Stream a ⚔️ challenge turn: *actor* contests *target*'s output.

    Mirrors :func:`stream_agent_turn` — best-effort, never raises.
    """
    try:
        stream_agent_turn(
            actor=actor,
            target=target,
            summary=point,
            icon="⚔️",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.challenge_failed", actor=actor, target=target, error=str(exc))


def stream_rebuttal(actor: str, target: str, point: str) -> None:
    """Stream a 🛡️ rebuttal turn: *actor* defends against *target*'s challenge.

    Best-effort, never raises.
    """
    try:
        stream_agent_turn(
            actor=actor,
            target=target,
            summary=point,
            icon="🛡️",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.rebuttal_failed", actor=actor, target=target, error=str(exc))


def stream_resolved(actor: str, target: str, resolution: str) -> None:
    """Stream a ⚖️ resolved turn: challenge accepted or defended and closed.

    Best-effort, never raises.
    """
    try:
        stream_agent_turn(
            actor=actor,
            target=target,
            summary=resolution,
            icon="⚖️",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.resolved_failed", actor=actor, target=target, error=str(exc))


def stream_needs_human(actor: str, target: str, point: str) -> None:
    """Stream a 🙋 needs-human turn: challenge escalated for human review.

    Best-effort, never raises.
    """
    try:
        stream_agent_turn(
            actor=actor,
            target=target,
            summary=point,
            icon="🙋",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.needs_human_failed", actor=actor, target=target, error=str(exc))


def send_approval_keyboard(
    run_id: int, project: str, task: str, details: str | None = None
) -> None:
    """Send an approval request with inline Approve/Deny buttons via Telegram and Slack."""
    # Choke point: `details` (checkpoint approval card) is frequently built
    # from `prior_chunks` — accumulated agent stage output — via
    # Orchestrator._build_checkpoint_details, so it can echo a resolved
    # ${secret:NAME} value. Redact before it reaches the Telegram DM.
    from hivepilot.services.config_provenance import redact_text

    # Outward consent (`notify`): an approval keyboard is an outbound
    # Telegram/Slack/Discord/Signal message like any other. Suppressing it
    # does NOT strand the approval -- the run still pauses and the request
    # is still visible in `hivepilot approvals`, the API and Pollen, which
    # are in-machine surfaces. The WARNING `permits` logs is what tells the
    # operator to look there instead of at their phone.
    if not outward.permits(
        "notify",
        surface="notification_service.send_approval_keyboard",
        detail=f"run {run_id}",
    ):
        return

    details = redact_text(details) if details is not None else details
    try:
        from hivepilot.services.telegram_bot import notify_approval_required

        notify_approval_required(run_id=run_id, project=project, task=task, details=details)
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

    try:
        # Phase 23e — Signal (text-only: Signal has no inline buttons, so
        # signal_bot.notify_approval_required sends "reply approve/deny
        # <run_id>" instructions instead of a keyboard). Same best-effort,
        # never-break-a-run shape as the Slack/Discord branches above.
        from hivepilot.services.signal_bot import notify_approval_required as signal_notify

        signal_notify(run_id=run_id, project=project, task=task)
    except Exception:  # noqa: BLE001
        pass


def stream_agent_request(requester: str, target: str, question: str) -> None:
    """Stream a ❓ request turn: *requester* asks *target* a targeted question.

    Best-effort, never raises.
    """
    try:
        stream_agent_turn(
            actor=requester,
            target=target,
            summary=question,
            icon="❓",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "stream.agent_request_failed",
            requester=requester,
            target=target,
            error=str(exc),
        )


def stream_agent_answer(target: str, requester: str, answer_excerpt: str) -> None:
    """Stream a ↩️ answer turn: *target* answers *requester*'s request.

    Best-effort, never raises.
    """
    try:
        stream_agent_turn(
            actor=target,
            target=requester,
            summary=answer_excerpt,
            icon="↩️",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "stream.agent_answer_failed",
            target=target,
            requester=requester,
            error=str(exc),
        )
