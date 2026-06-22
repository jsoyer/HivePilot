from __future__ import annotations

from datetime import datetime, timezone

from hivepilot.services.quota import QuotaError, parse_quota_error

REAL_QUOTA_MSG = "claude exited 1: You've hit your session limit · resets 9:40pm (Europe/Paris)"


def test_parse_real_quota_message():
    """Matches the real claude CLI session-limit message."""
    result = parse_quota_error(REAL_QUOTA_MSG)
    assert result is not None
    assert isinstance(result, QuotaError)
    assert result.raw == REAL_QUOTA_MSG


def test_reset_time_extracted_when_in_future():
    """9:40pm → datetime later than the provided 'now'."""
    # Use a fixed 'now' that is before 9:40pm UTC
    now = datetime(2026, 6, 22, 18, 0, 0, tzinfo=timezone.utc)  # 6:00pm UTC
    result = parse_quota_error(REAL_QUOTA_MSG, now=now)
    assert result is not None
    assert result.reset_at is not None
    assert result.reset_at > now
    assert result.reset_at.hour == 21  # 9pm (21:40 UTC)
    assert result.reset_at.minute == 40


def test_reset_time_tomorrow_when_past():
    """If 9:40pm is already past, next occurrence is tomorrow."""
    now = datetime(2026, 6, 22, 22, 0, 0, tzinfo=timezone.utc)  # 10:00pm UTC — after 9:40pm
    result = parse_quota_error(REAL_QUOTA_MSG, now=now)
    assert result is not None
    assert result.reset_at is not None
    assert result.reset_at > now
    assert result.reset_at.day == 23  # tomorrow


def test_non_quota_message_returns_none():
    """A normal error message (not quota) returns None."""
    assert parse_quota_error("RuntimeError: command failed with exit code 1") is None
    assert parse_quota_error("claude exited 1: Syntax error in prompt") is None
    assert parse_quota_error("Connection timeout") is None


def test_missing_time_gives_none_reset_at():
    """A quota message without a parseable reset time → reset_at=None."""
    msg = "You've hit your session limit. Please try again later."
    result = parse_quota_error(msg)
    assert result is not None
    assert result.reset_at is None


def test_usage_limit_variant():
    """'usage limit' keyword also triggers quota detection."""
    msg = "You've hit your usage limit · resets 3:50pm"
    now = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    result = parse_quota_error(msg, now=now)
    assert result is not None
    assert result.reset_at is not None


def test_rate_limit_variant():
    """'rate limit' keyword also triggers quota detection."""
    msg = "rate limit exceeded · resets 2:15am"
    now = datetime(2026, 6, 22, 22, 0, 0, tzinfo=timezone.utc)
    result = parse_quota_error(msg, now=now)
    assert result is not None
    assert result.reset_at is not None


def test_case_insensitive_matching():
    """Detection is case-insensitive."""
    msg = "You've hit your SESSION LIMIT · RESETS 9:40PM (Europe/Paris)"
    result = parse_quota_error(msg)
    assert result is not None
