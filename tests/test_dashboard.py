"""Tests for hivepilot.ui.dashboard — skipped when textual is not installed."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual.app")

from hivepilot.ui.dashboard import RunDashboard  # noqa: E402


def test_refresh_interactions_method_exists() -> None:
    assert hasattr(RunDashboard, "refresh_interactions")


def test_refresh_interactions_is_callable() -> None:
    assert callable(getattr(RunDashboard, "refresh_interactions"))
