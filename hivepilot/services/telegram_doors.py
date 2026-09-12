"""Pollen four-door Telegram forum layout (HP-92).

Persistent topics match the Pollen web doors. Telegram's built-in General
topic is never treated as a catch-all: unmatched, closed, or threadless
traffic in the stream forum lands on Inbox (or Alerts when the payload is
failed / degraded / classifier).

Ephemeral RUN topics are allowed: ``{emoji} {slug}`` with the run id in the
first message. Role colors stay out of chrome. Do not invent emoji pairs —
hand-offs use the eight role-charte glyphs; system / concierge uses 🐝.
"""

from __future__ import annotations

import html
import re
from typing import Any

from hivepilot.services.telegram_avatars import (
    CANONICAL_ROLE_EMOJI,
    fallback_emoji,
    html_mark,
    role_key_from_actor,
)

INBOX = "inbox"
APPROVALS = "approvals"
RUNS = "runs"
ALERTS = "alerts"

PERSISTENT_DOORS: tuple[str, ...] = (INBOX, APPROVALS, RUNS, ALERTS)

DOOR_TITLES: dict[str, str] = {
    INBOX: "Inbox",
    APPROVALS: "Approvals",
    RUNS: "Runs",
    ALERTS: "Alerts",
}

SYSTEM_EMOJI = "🐝"

INBOX_WELCOME_HTML = (
    f"{SYSTEM_EMOJI} <b>Inbox</b>\n"
    "Talk · classify · confirm\n"
    "Approvals, Runs, and Alerts have their own topics — this is not a dump."
)

_RUN_KEY_PREFIX = "run:"

_ALERT_MARKERS: tuple[str, ...] = (
    "failed",
    "failure",
    "error",
    "degraded",
    "classifier",
    "❌",
    "⛔",
    "crash",
    "blocked",
)


def door_title(key: str) -> str | None:
    """Pollen door title, or ``None`` when *key* is not a persistent door."""
    return DOOR_TITLES.get(key)


def is_persistent_door(key: str) -> bool:
    return key in DOOR_TITLES


def is_run_topic_key(key: str) -> bool:
    return key.startswith(_RUN_KEY_PREFIX) and bool(key[len(_RUN_KEY_PREFIX) :].strip())


def run_topic_key(run_id: int) -> str:
    return f"{_RUN_KEY_PREFIX}{int(run_id)}"


def slugify(value: str, *, fallback: str = "run") -> str:
    """Short topic slug — letters, digits, hyphen; never a role title."""
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:24] or fallback).rstrip("-")


def run_topic_title(role_key: str | None, slug: str) -> str:
    """``{emoji} {slug}`` — role-charte emoji, or 🐝 for system/unknown."""
    emoji = fallback_emoji(role_key) or SYSTEM_EMOJI
    return f"{emoji} {slugify(slug)}"


def run_first_message_html(run_id: int, slug: str) -> str:
    """First message in an ephemeral RUN topic — id lives here, not the title."""
    safe_slug = html.escape(slugify(slug))
    return f"<b>run #{int(run_id)}</b>\n{safe_slug}"


def classify_notification_door(message: str) -> str:
    """Failed / degraded / classifier → Alerts; everything else → Inbox."""
    low = (message or "").lower()
    if any(marker in low for marker in _ALERT_MARKERS):
        return ALERTS
    return INBOX


def speaker_html(actor: str) -> str:
    """Role-charte mark, or 🐝 for system / concierge / unmatched actors."""
    mark = html_mark(role_key_from_actor(actor))
    return mark if mark else f"{SYSTEM_EMOJI} "


def speaker_plain(actor: str) -> str:
    role_key = role_key_from_actor(actor)
    emoji = fallback_emoji(role_key) or SYSTEM_EMOJI
    return f"{emoji} "


def render_soft_card(
    *,
    actor: str,
    target: str | None = None,
    status: str | None = None,
    meta: str | None = None,
) -> str:
    """Bold title plus at most two meta lines. No chrome colors."""
    lines = [f"{speaker_html(actor)}<b>{html.escape(actor)}</b>"]
    meta1_parts: list[str] = []
    if target:
        meta1_parts.append(html.escape(target))
    if status:
        meta1_parts.append(html.escape(status))
    if meta1_parts:
        lines.append(" · ".join(meta1_parts))
    if meta:
        lines.append(html.escape(meta))
    return "\n".join(lines[:3])


def soft_card_from_report(
    *,
    actor: str,
    target: str | None,
    report: Any,
) -> str:
    """Softer stream card: title + two meta lines from a parsed report."""
    from hivepilot.services.agent_report import to_telegram_text

    status = report.status if getattr(report, "status", None) else None
    meta: str | None = None
    summary = getattr(report, "summary", None) or []
    if summary:
        first = to_telegram_text(summary[0]).strip()
        if first:
            meta = first
    if not meta and getattr(report, "next_handoff", None):
        meta = f"next: {report.next_handoff}"
    elif not meta and getattr(report, "confidence", None):
        meta = f"confidence: {report.confidence}"
    if not meta:
        links = getattr(report, "links", None) or []
        artifact = next(
            (lnk for lnk in links if str(lnk).endswith(".md") and not str(lnk).startswith("http")),
            None,
        )
        if artifact:
            meta = str(artifact)
    return render_soft_card(actor=actor, target=target, status=status, meta=meta)


def concierge_answer_text(answer: str) -> str:
    """ANSWER replies are clean text — no keyboard, 🐝 prefix."""
    body = (answer or "").strip() or "I'm not sure how to help with that. Try /help."
    if body.startswith(SYSTEM_EMOJI):
        return body
    return f"{SYSTEM_EMOJI} {body}"


def concierge_action_prompt(summary: str) -> str:
    """ACTION confirm body — buttons are attached by the caller, not here."""
    return f"{SYSTEM_EMOJI} Confirm\n{summary}\nYes runs it · No cancels"


# Re-export the charte so callers do not invent pairs.
ROLE_CHARTE_EMOJI = CANONICAL_ROLE_EMOJI
