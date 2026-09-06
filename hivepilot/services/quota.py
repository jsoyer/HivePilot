from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class QuotaError:
    raw: str
    reset_at: datetime | None


class QuotaDeferredError(Exception):
    """Raised by _execute_task when quota is exhausted and all fallbacks are tried.

    The caller (run_task fan-out) catches this to enqueue a deferred retry
    instead of treating it as a hard failure.
    """

    def __init__(self, message: str, reset_at: "datetime | None" = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


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


# Indicators that a model/provider is UNAVAILABLE for reasons other than a
# quota/rate limit — no credit, bad auth, or the model/service being down.
# Unlike a quota limit these have NO reset time, so the fallback loop should
# fail OVER to the next model but must NOT defer-and-retry-later on exhaustion.
# Kept deliberately specific to avoid misclassifying a genuine code/tool error
# as "provider unavailable" (worst case of a false positive is a needless
# fallback attempt; a false positive that SWALLOWED a real bug would be worse).
_UNAVAILABLE_INDICATORS = (
    # no credit / payment
    "insufficient credit",
    "insufficient_quota",
    "out of credit",
    "no credit",
    "creditserror",
    "credit balance is too low",
    "payment required",
    "402",
    # auth
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "unauthorized",
    "authenticationerror",
    "authentication_error",
    "auth error",
    "401",
    # model / service unavailable
    "model not found",
    "model_not_found",
    "no such model",
    "service unavailable",
    "503",
    "overloaded",
)


def is_provider_unavailable(message: str) -> bool:
    """True when `message` indicates the model/provider is unavailable for a
    NON-quota reason (no credit, bad auth, model/service down). Used to widen
    the fallback trigger beyond quota (`parse_quota_error`) so a role's model
    chain also fails over when the primary has no credit or bad auth — the
    exact `CreditsError`/`AuthError` cases hit with OpenCode Zen. A quota
    message is NOT this (it has a reset time and defers instead); classify
    quota first."""
    lower = message.lower()
    return any(indicator in lower for indicator in _UNAVAILABLE_INDICATORS)
