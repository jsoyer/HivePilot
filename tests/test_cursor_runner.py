"""CursorRunner capability contract (Sprint 1: mode cli|api).

CursorRunner subclasses PromptCliRunner (which advertises cli+api), but the
cursor-agent path is CLI-only in HivePilot, so it must OVERRIDE back to
cli-only — otherwise a resolved mode:api would silently take an unsupported
provider-API path instead of failing closed at orchestrator validation.
"""

from __future__ import annotations

from hivepilot.runners.cursor_runner import CursorRunner
from hivepilot.runners.prompt_cli_runner import PromptCliRunner


def test_cursor_runner_overrides_to_cli_only() -> None:
    assert CursorRunner.supported_modes == frozenset({"cli"})
    # Sanity: the parent still advertises api support — cursor overrides it.
    assert PromptCliRunner.supported_modes == frozenset({"cli", "api"})
