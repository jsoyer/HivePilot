from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class QuotaError:
    raw: str
    reset_at: datetime | None


def parse_quota_error(message: str, now: datetime | None = None) -> QuotaError | None:
    """Parse a claude CLI quota/rate-limit error message.

    Returns QuotaError if the message contains a session limit / usage limit / rate limit
    indicator. Returns None for non-quota messages.

    The reset_at datetime is the NEXT occurrence of the parsed wall-clock time:
    today if still future, tomorrow otherwise. If no parseable time, reset_at=None.

    Args:
        message: The error message string to parse.
        now: Optional override for "current time" (defaults to datetime.now(timezone.utc)).
             Pass this to make the "next occurrence" computation testable.
    """
    lower = message.lower()
    # Must contain a quota/limit indicator AND a "resets" marker
    has_limit = any(phrase in lower for phrase in ("session limit", "usage limit", "rate limit"))
    has_resets = "resets" in lower
    if not has_limit:
        return None

    # Parse reset time from patterns like "resets 9:40pm (Europe/Paris)" or "resets 3:50pm"
    reset_at: datetime | None = None
    if has_resets:
        # Match time like "9:40pm" or "3:50pm" optionally followed by timezone in parens
        time_match = re.search(r"resets\s+(\d{1,2}):(\d{2})(am|pm)", message, re.IGNORECASE)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            ampm = time_match.group(3).lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0

            if now is None:
                now = datetime.now(timezone.utc)

            # Build reset datetime for today (UTC)
            reset_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If still in the future today, use today; otherwise use tomorrow
            if reset_today > now:
                reset_at = reset_today
            else:
                reset_at = reset_today + timedelta(days=1)

    return QuotaError(raw=message, reset_at=reset_at)
