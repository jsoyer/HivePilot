from __future__ import annotations

import html
import json
import os
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

import requests

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Max chars of an agent's output shown inline in a Telegram turn (Telegram caps
# messages at ~4096; keep headroom for the header lines).
_STREAM_MAX_CHARS = 1500

# Telegram HTML card: keep total under this (Telegram limit is ~4096 bytes).
_RICH_MAX_CHARS = 3500

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

# Path for persisting agent_key -> message_thread_id across runs.
_TOPICS_REGISTRY_PATH = Path(".hivepilot/stream_topics.json")

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
    requests.post(url, json=payload, timeout=5)


NotifierRegistry.register("slack", _send_slack)
NotifierRegistry.register("discord", _send_discord)
NotifierRegistry.register("telegram", _send_telegram)


def _load_topics() -> dict[str, int]:
    """Load the agent_key -> message_thread_id registry from disk. Best-effort."""
    try:
        path = _TOPICS_REGISTRY_PATH
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.topics.load_failed", error=str(exc))
    return {}


def _save_topics(mapping: dict[str, int]) -> None:
    """Persist the topics registry to disk. Best-effort."""
    try:
        path = _TOPICS_REGISTRY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("stream.topics.save_failed", error=str(exc))


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

    Returns a string ready for ``parse_mode="HTML"``.
    All user-derived text is escaped via ``html.escape``.
    Total length is kept under ``_RICH_MAX_CHARS``.
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

    # Summary bullets (max 5), cleaned and truncated
    from hivepilot.services.agent_report import to_telegram_text

    # Find vault artifact link (any .md path in report.links)
    artifact_link: str | None = next(
        (lnk for lnk in report.links if lnk.endswith(".md") and not lnk.startswith("http")),
        None,
    )

    bullet_lines: list[str] = []
    summary_chars = 0
    _SUMMARY_MAX = 700
    for bullet in report.summary[:5]:
        clean = to_telegram_text(bullet).strip()
        if not clean:
            continue
        if len(clean) > 180:
            clean = clean[:179] + "…"
        rendered = f"• {html.escape(clean)}"
        if summary_chars + len(rendered) + 1 > _SUMMARY_MAX:
            # Over budget — add truncation notice and stop
            notice = "… (full details in the vault artifact)"
            if artifact_link:
                safe = html.escape(artifact_link)
                notice += f' <a href="file://{safe}">{safe}</a>'
            bullet_lines.append(notice)
            break
        bullet_lines.append(rendered)
        summary_chars += len(rendered) + 1  # +1 for the newline

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

    card = "\n".join(lines)

    # Truncate if needed — drop trailing bullets (never mid-tag)
    if len(card) > _RICH_MAX_CHARS:
        # Rebuild without links first
        lines_no_links = [ln for ln in lines if not ln.startswith("<a href=")]
        card = "\n".join(lines_no_links)
        if len(card) > _RICH_MAX_CHARS:
            card = card[: _RICH_MAX_CHARS - 1] + "…"

    return card


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
    instead of plain text.
    """
    if not settings.telegram_stream_live:
        return

    message_thread_id: int | None = None
    if settings.telegram_stream_topics and settings.telegram_stream_chat_id:
        agent_key = _resolve_agent_key(actor)
        message_thread_id = _ensure_topic_thread(agent_key, f"{actor}")

    # --- Attempt rich HTML card rendering ---
    use_rich = getattr(settings, "telegram_stream_rich", True)
    send_kwargs: dict[str, Any] = {
        "chat_id": settings.telegram_stream_chat_id,
        "message_thread_id": message_thread_id,
    }
    message_text: str | None = None
    parse_mode: str | None = None

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
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream.rich_render_failed", error=str(exc))
            # Fall through to plain text

    # --- Plain-text fallback ---
    if message_text is None:
        label = _ICON_LABELS.get(icon)
        tag = f"{icon} ({label})" if label else icon
        header = f"{tag} {actor}" + (f" — {stage}" if stage else "")
        plain_lines = [header]
        if target:
            plain_lines.append(f"   ↳ {target}")
        if summary:
            snippet = " ".join(summary.split())
            if len(snippet) > _STREAM_MAX_CHARS:
                snippet = snippet[: _STREAM_MAX_CHARS - 1] + "…"
            if snippet:
                plain_lines.append(f"   {snippet}")
        message_text = "\n".join(plain_lines)

    try:
        # Live agent stream goes to its dedicated channel when set, else falls
        # back to the main notification chat.
        _send_telegram(
            message_text,
            chat_id=send_kwargs["chat_id"],
            message_thread_id=send_kwargs["message_thread_id"],
            parse_mode=parse_mode,
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
