"""Tests for L2 build_prior_context helper (cap / synthesis / full modes)."""

from __future__ import annotations

import pytest

from hivepilot.orchestrator import build_prior_context


def test_build_prior_context_full() -> None:
    chunks = ["A", "B", "C"]
    result = build_prior_context(chunks, "full", 9999)
    assert result == "A\n\nB\n\nC"


def test_build_prior_context_empty() -> None:
    assert build_prior_context([], "cap", 9999) is None


def test_build_prior_context_cap_no_truncation() -> None:
    chunks = ["short"]
    result = build_prior_context(chunks, "cap", 9999)
    assert result == "short"


def test_build_prior_context_cap_truncates_and_keeps_tail() -> None:
    chunks = ["AAAA", "BBBB"]
    full = "AAAA\n\nBBBB"  # len=10
    result = build_prior_context(chunks, "cap", 6)
    assert result is not None
    assert "…[earlier context truncated]…" in result
    # tail of full[-6:] = "\nBBBB" -> result ends with that
    assert result.endswith(full[-6:])
    # head should NOT appear (was truncated)
    assert "AAAA" not in result


def test_build_prior_context_synthesis_picks_synthesis_plus_last() -> None:
    chunks = [
        "## Plan Synthesis (plan)\nthe plan",
        "## Jules (review)\nthe review",
        "## Theo (implement)\nthe impl",
    ]
    result = build_prior_context(chunks, "synthesis", 9999)
    assert result is not None
    assert "Plan Synthesis" in result
    assert "the impl" in result
    # middle chunk should NOT be in result
    assert "the review" not in result


def test_build_prior_context_synthesis_no_synthesis_chunk_keeps_last() -> None:
    chunks = ["## A\nfoo", "## B\nbar"]
    result = build_prior_context(chunks, "synthesis", 9999)
    assert result is not None
    assert "bar" in result
    assert "foo" not in result


def test_build_prior_context_synthesis_last_is_synthesis() -> None:
    """When the synthesis chunk IS the last chunk, no duplication."""
    chunks = [
        "## Jules (review)\nthe review",
        "## Plan Synthesis (plan)\nthe plan",
    ]
    result = build_prior_context(chunks, "synthesis", 9999)
    assert result is not None
    assert "the plan" in result
    # synthesis chunk is also the last → should appear once only
    assert result.count("the plan") == 1


def test_build_prior_context_full_empty_returns_none() -> None:
    assert build_prior_context([], "full", 9999) is None
