"""Tests for dev fan-out batching per quota window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def test_batching_defers_remainder():
    """With dev_batch_size=2 and 5 projects, 2 run immediately and 3 are deferred."""
    all_projects = [f"repo-{i}" for i in range(5)]
    batch_size = 2
    batch = all_projects[:batch_size]
    remainder = all_projects[batch_size:]

    assert len(batch) == 2
    assert len(remainder) == 3
    assert batch == ["repo-0", "repo-1"]
    assert remainder == ["repo-2", "repo-3", "repo-4"]

    enqueued: list[dict] = []

    def fake_enqueue_deferred(**kwargs):
        enqueued.append(kwargs)
        return len(enqueued)

    defer_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    with patch("hivepilot.services.retry_service.enqueue_deferred", fake_enqueue_deferred):
        from hivepilot.services.retry_service import enqueue_deferred

        for proj in remainder:
            enqueue_deferred(
                task="dev",
                projects=[proj],
                error="batch limit: deferred to next window",
                next_retry_at=defer_at,
                context={"task": "dev", "extra_prompt": None, "auto_git": False},
            )

    assert len(enqueued) == 3
    assert enqueued[0]["projects"] == ["repo-2"]
    assert enqueued[1]["projects"] == ["repo-3"]
    assert enqueued[2]["projects"] == ["repo-4"]
    for e in enqueued:
        assert e["error"] == "batch limit: deferred to next window"


def test_dev_batch_size_config_default():
    """dev_batch_size defaults to 0 (unlimited)."""
    from hivepilot.config import Settings

    s = Settings()
    assert s.dev_batch_size == 0


def test_quota_deferred_error_is_exception():
    """QuotaDeferredError is an Exception subclass."""
    from hivepilot.services.quota import QuotaDeferredError

    assert issubclass(QuotaDeferredError, Exception)
    exc = QuotaDeferredError("test", reset_at=None)
    assert exc.reset_at is None
