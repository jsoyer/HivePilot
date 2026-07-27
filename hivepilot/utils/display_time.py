"""Single conversion helper for rendering stored UTC timestamps to a human
operator in their configured/local display timezone.

THE BUG THIS FIXES: `state_service` stores every timestamp via SQLite's
`CURRENT_TIMESTAMP` (UTC, naive — no offset, no 'Z', no timezone marker at
all: e.g. ``"2026-07-27 09:08:32"``). Storage is correct: UTC is the right
absolute instant of truth and this module never changes it. The regression
was that every HUMAN-facing surface (Telegram/Slack/Discord/Signal chat
replies, the NL concierge, CLI tables) printed that raw string verbatim —
which LOOKS exactly like local time to a human reader, so an operator in
Europe/Paris (CEST, UTC+2) read "09:08" and searched their logs two hours
off from the real 11:08 local event.

This module is the ONE place that:
  1. Parses a stored value (naive-UTC string, aware-ISO string, or an aware/
     naive ``datetime``) into an aware UTC ``datetime``.
  2. Resolves the *display* timezone: an explicit override
     (``Settings.display_timezone`` / ``HIVEPILOT_DISPLAY_TIMEZONE``) first,
     else the HOST's detected system timezone, else UTC as a last resort —
     clearly marked as such so this class of bug can never hide again.
  3. Renders the result with an explicit timezone marker (abbreviation, e.g.
     "CEST", or a numeric UTC offset when no abbreviation is available) — a
     bare, unmarked time is exactly how the original bug hid.

Every human-facing sink (``telegram_bot``, ``discord_bot``, ``slack_bot``,
``chatops_service``, ``concierge_service``, ``cli.py``) calls
:func:`to_display` instead of interpolating a raw stored value — one
mechanism, not one per channel.

MACHINE-facing output (API JSON, logs, anything another program parses)
MUST NOT go through this module — it stays the raw UTC value
``state_service`` already returns/writes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Module-level so tests can monkeypatch them without touching the real
# filesystem — see detect_system_zone_name().
_ETC_TIMEZONE_PATH = Path("/etc/timezone")
_ETC_LOCALTIME_PATH = Path("/etc/localtime")

_ZONEINFO_ROOT_CANDIDATES = ("/usr/share/zoneinfo", "/etc/zoneinfo")

# Rendered when a value can't be parsed at all (None, empty, garbage) —
# never crash a chat reply or CLI table over one bad row.
UNKNOWN_DISPLAY = "(unknown)"

_DEFAULT_FMT = "%Y-%m-%d %H:%M %Z"


def _zone_name_from_localtime_symlink() -> str | None:
    """Resolve ``/etc/localtime``'s symlink target to an IANA zone name
    (e.g. "Europe/Paris") — the standard mechanism on virtually every Linux
    distro, including Alpine once tzdata/setup-timezone has run. Returns
    None if `/etc/localtime` is missing, isn't a symlink, or doesn't point
    inside a recognized zoneinfo root."""
    try:
        if not _ETC_LOCALTIME_PATH.is_symlink():
            return None
        target = str(_ETC_LOCALTIME_PATH.resolve())
    except OSError:
        return None
    for root in _ZONEINFO_ROOT_CANDIDATES:
        prefix = root + "/"
        if target.startswith(prefix):
            return target[len(prefix) :]
    return None


def _zone_name_from_etc_timezone() -> str | None:
    """Debian/Ubuntu-style ``/etc/timezone`` (a single line, e.g.
    "Europe/Paris"). Alpine ships this too when tzdata is configured."""
    try:
        content = _ETC_TIMEZONE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def detect_system_zone_name() -> str | None:
    """Best-effort IANA zone name for the HOST this process runs on, tried
    in order: ``TZ`` env var, ``/etc/timezone``, ``/etc/localtime``'s
    symlink target.

    Deliberately does NOT fall back to
    ``datetime.now().astimezone().tzinfo`` — that only captures the
    CURRENT fixed UTC offset, not the zone's DST rules, and would silently
    mis-render any stored timestamp outside the current DST period (exactly
    the class of bug this module exists to fix). Returns None if no method
    succeeds — callers must surface that (via ``resolve_display_zone``'s
    ``"fallback-utc"`` source and the ``config doctor`` check), never
    silently default to UTC without saying so.
    """
    env_tz = os.environ.get("TZ")
    if env_tz:
        return env_tz
    return _zone_name_from_etc_timezone() or _zone_name_from_localtime_symlink()


def _resolve_zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def resolve_display_zone() -> tuple[ZoneInfo | timezone, str]:
    """Resolve the zone every human-facing sink should render into.

    Returns ``(zone, source)`` where ``source`` is one of:
      * ``"override"`` — ``Settings.display_timezone`` resolved cleanly.
      * ``"system"`` — no (valid) override; the host's system timezone was
        detected.
      * ``"fallback-utc"`` — neither resolved; rendering falls back to UTC.
        This is the exact condition `check_display_timezone` (config
        doctor) flags as a WARNING, since it means every rendered timestamp
        will look like local time while actually being UTC — the original
        bug, generalised.

    An invalid override name (typo) does NOT crash every render call; it
    falls through to system detection, but is itself a real
    misconfiguration the doctor check reports as an ERROR.
    """
    from hivepilot.config import settings

    override = settings.display_timezone
    if override:
        zone = _resolve_zone(override)
        if zone is not None:
            return zone, "override"
        logger.warning("display_time.invalid_override", display_timezone=override)

    system_name = detect_system_zone_name()
    zone = _resolve_zone(system_name)
    if zone is not None:
        return zone, "system"

    return timezone.utc, "fallback-utc"


def _parse_stored(value: datetime | str) -> datetime | None:
    """Parse a stored timestamp into an aware UTC ``datetime``. A naive
    value (naive ``datetime`` OR a string with no offset — exactly what
    SQLite's ``CURRENT_TIMESTAMP`` writes, matching every writer in
    ``state_service.py``) is ASSUMED to be UTC. Returns None if ``value``
    is empty/unparseable."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_offset(offset) -> str:
    if offset is None:
        return "+00:00"
    total_minutes = round(offset.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def to_display(
    value: datetime | str | None,
    *,
    zone: ZoneInfo | timezone | None = None,
    fmt: str = _DEFAULT_FMT,
) -> str:
    """Render a stored UTC timestamp for a HUMAN reader.

    Converts to the display timezone (explicit override > detected system
    zone > UTC fallback — see ``resolve_display_zone``) and marks the
    result with its abbreviation or numeric UTC offset, so it never again
    reads as an unmarked, ambiguous local time — a bare time with no marker
    is exactly how the original bug hid.

    Returns ``UNKNOWN_DISPLAY`` for ``None``/empty/unparseable input rather
    than raising — a bad or missing timestamp on one row must never crash a
    chat reply or CLI table render.

    NEVER use this for machine-facing output (API JSON, logs) — those must
    stay the raw UTC value ``state_service`` already returns.
    """
    if value is None:
        return UNKNOWN_DISPLAY
    parsed = _parse_stored(value)
    if parsed is None:
        return UNKNOWN_DISPLAY

    resolved_zone = zone if zone is not None else resolve_display_zone()[0]
    local = parsed.astimezone(resolved_zone)

    if local.tzname():
        return local.strftime(fmt)
    # Some fixed-offset zones report an empty abbreviation — fall back to a
    # numeric UTC offset so the "always mark the timezone" guarantee holds.
    return f"{local.strftime('%Y-%m-%d %H:%M')} UTC{_format_offset(local.utcoffset())}"
