"""ShellRunner capability contract (Sprint 1: mode cli|api)."""

from __future__ import annotations

from hivepilot.runners.shell_runner import ShellRunner


def test_shell_runner_is_cli_only() -> None:
    """A non-agent runner must advertise cli-only so a resolved mode:api fails
    closed at orchestrator validation before any subprocess is spawned."""
    assert ShellRunner.supported_modes == frozenset({"cli"})
