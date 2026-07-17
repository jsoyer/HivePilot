"""InternalRunner capability contract (Sprint 1: mode cli|api)."""

from __future__ import annotations

from hivepilot.runners.internal_runner import InternalRunner


def test_internal_runner_is_cli_only() -> None:
    assert InternalRunner.supported_modes == frozenset({"cli"})
