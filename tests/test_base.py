"""Tests for hivepilot.runners.base — UsageInfo + last-usage stash helpers.

Phase 24b.2a — opt-in usage capture. The stash (ContextVar-backed) lets a
runner's ``capture()`` hand token/cost/model usage back to its caller without
changing ``capture()``'s ``str`` return contract.
"""

from __future__ import annotations

from hivepilot.runners.base import UsageInfo, pop_last_usage, set_last_usage


def test_usage_info_defaults_all_none() -> None:
    usage = UsageInfo()
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cost_usd is None
    assert usage.model is None


def test_usage_info_is_frozen() -> None:
    usage = UsageInfo(input_tokens=1)
    try:
        usage.input_tokens = 2  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised, "UsageInfo must be immutable (frozen dataclass)"


def test_pop_last_usage_defaults_to_none() -> None:
    """Nothing stashed yet -> None, never an invented value."""
    assert pop_last_usage() is None


def test_set_then_pop_returns_the_stashed_usage() -> None:
    usage = UsageInfo(input_tokens=10, output_tokens=20, cost_usd=0.01, model="claude-x")
    set_last_usage(usage)
    assert pop_last_usage() is usage


def test_pop_clears_the_stash() -> None:
    """A second pop after the first must return None -- no stale leakage
    into whatever step reads next."""
    set_last_usage(UsageInfo(input_tokens=1))
    pop_last_usage()
    assert pop_last_usage() is None


def test_set_none_clears_stash() -> None:
    set_last_usage(UsageInfo(input_tokens=1))
    set_last_usage(None)
    assert pop_last_usage() is None
