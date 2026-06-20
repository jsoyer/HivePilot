"""Unit tests for PromptCliRunner subclass defaults.

The non-interactive invocation contract (actual argv built per runner) is locked
in test_runner_invocation.py. Here we just pin the declared class defaults for the
Mistral ``vibe`` runner so a future refactor can't silently drop its flags.
"""

from __future__ import annotations

from hivepilot.runners.prompt_cli_runner import VibeRunner


def test_vibe_runner_defaults() -> None:
    assert VibeRunner.command_name == "vibe"
    assert "--auto-approve" in VibeRunner.cli_flags
    assert VibeRunner.prompt_flag == "--prompt"
    # vibe has no subcommand (unlike codex 'exec' / opencode 'run')
    assert VibeRunner.cli_subcommand is None
