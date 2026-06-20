"""Tests for the runner registry mapping."""

from __future__ import annotations

from hivepilot.registry import RUNNER_MAP
from hivepilot.runners.prompt_cli_runner import VibeRunner


def test_vibe_kind_is_registered() -> None:
    assert RUNNER_MAP.get("vibe") is VibeRunner
