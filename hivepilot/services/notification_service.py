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

from hivepilot.config import settings
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
    # unbalanced/unsupported entity our formatter didn't anticipate). Raise
    # so callers like _send_chunks can retry that one chunk as plain text
    # instead of the failure silently vanishing.
    resp.raise_for_status()


NotifierRegistry.register("slack", _send_slack)
NotifierRegistry.register("discord", _send_discord)
NotifierRegistry.register("telegram", _send_telegram)


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
# "&lt;" by the time this regex runs and can never masquerade as a tag.
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


def _find_break(text: str, budget: int) -> int:
    """Return the best index <= *budget* to cut *text* at.

    Prefers a paragraph break (``\\n\\n``), then a line break (``\\n``),
    then a word boundary (space) — never mid-word if one of those exists
    within the budget. Falls back to a hard cut at *budget* only when none
    of the softer boundaries are available.
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


def _tokenize_html(text: str) -> list[str]:
    """Split *text* into a flat token list: each ``<tag>``/``</tag>`` is its
    own atomic token (never split), everything else is a plain-text token
    that the packer in :func:`_split_for_telegram` may split further."""
    tokens: list[str] = []
    last = 0
    for m in _TAG_TOKEN_RE.finditer(text):
        if m.group(2) not in _KNOWN_TAGS:
            continue  # not one of ours — treat as plain text, don't tokenize
        if m.start() > last:
            tokens.append(text[last : m.start()])
        tokens.append(m.group(0))
        last = m.end()
    if last < len(text):
        tokens.append(text[last:])
    return tokens


def _split_for_telegram(
    text: str,
    limit: int = _SPLIT_LIMIT,
    max_chunks: int = _MAX_CHUNKS,
    *,
    html_aware: bool = True,
) -> list[str]:
    """Split *text* into ordered Telegram-sized chunks — never truncates.

    Breaks on a paragraph boundary first, then a line boundary, then a word
    boundary (see :func:`_find_break`). When *html_aware* (the default —
    pass ``False`` for genuinely plain, non-HTML text so literal ``<``/``>``
    in agent output, e.g. ``List<int>``, is never mistaken for a tag), any
    of our own ``<b>``/``<i>``/``<code>``/``<pre>``/``<a>`` tags that would
    straddle a chunk boundary are closed at the end of the chunk they start
    in and re-opened at the start of the next one, so every returned chunk
    is independently well-formed HTML.

    Capped at *max_chunks* — a pathological (multi-hundred-KB) agent dump
    must never turn into dozens of Telegram messages; a short note is
    appended to the last chunk when content had to be dropped for the cap.
    When there is more than one chunk, each gets a trailing ``(i/N)``
    continuation marker.
    """
    if len(text) <= limit:
        return [text]

    tokens = _tokenize_html(text) if html_aware else [text]
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
            tag_match = _TAG_TOKEN_RE.fullmatch(tok) if html_aware else None

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
                if len(prefix) + len(current) + len(tok) + suffix_len > limit and current:
                    break  # finalize this chunk before the tag
                current += tok
                cur_stack = trial_stack
                idx += 1
                continue

            suffix_len = sum(len(f"</{t}>") for t in reversed(cur_stack))
            budget = max(limit - len(prefix) - len(current) - suffix_len, 0 if current else 1)
            if budget <= 0:
                break
            if len(tok) <= budget:
                current += tok
                idx += 1
                continue
            cut = _find_break(tok, budget)
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


def _send_chunks(
    text: str,
    *,
    chat_id: Any,
    message_thread_id: int | None,
    parse_mode: str | None,
    html_aware: bool,
) -> None:
    """Split *text* and send each chunk, in order, to the same chat + topic.

    If a formatted (``parse_mode="HTML"``) chunk is rejected by Telegram
    (a 400 — typically a parse-entity edge case), that ONE chunk is retried
    as plain text so the content always reaches the operator even when
    formatting doesn't. ``_NotConfigured`` always propagates unchanged so
    the caller's existing best-effort no-op behaviour is preserved.
    """
    for chunk in _split_for_telegram(text, html_aware=html_aware):
        try:
            _send_telegram(
                chunk,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                parse_mode=parse_mode,
            )
        except _NotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001
            if parse_mode is None:
                raise
            logger.warning("stream.chunk_send_failed_retry_plain", error=str(exc))
            _send_telegram(
                _strip_html(chunk),
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                parse_mode=None,
            )


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
    if settings.telegram_stream_topics and settings.telegram_stream_chat_id:
        agent_key = _resolve_agent_key(actor)
        message_thread_id = _ensure_topic_thread(agent_key, f"{actor}")

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
        )
    except _NotConfigured:
        pass  # Telegram not set up — streaming is best-effort
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.failed", error=str(exc))


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
