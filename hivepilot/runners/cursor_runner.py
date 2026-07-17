"""CursorRunner — wraps the `cursor-agent` CLI tool.

Mirrors the minimal subclass pattern used by CodexRunner / GeminiRunner /
OpenCodeRunner / OllamaRunner in prompt_cli_runner.py.

Includes a binary-availability guard: call _check_binary() (or it is called
automatically before run()) so that a missing `cursor-agent` installation
raises a clear RuntimeError naming the binary, rather than a cryptic
subprocess failure.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from hivepilot.runners.prompt_cli_runner import PromptCliRunner


@dataclass
class CursorRunner(PromptCliRunner):
    # cli-only override: PromptCliRunner advertises cli+api, but the
    # cursor-agent path is CLI-only in HivePilot — a resolved mode:api must
    # fail closed at orchestrator validation rather than take the API path.
    supported_modes = frozenset({"cli"})
    command_name: str = "cursor-agent"
    cli_flags: tuple[str, ...] = ("--print",)

    def _check_binary(self) -> None:
        """Raise RuntimeError if the cursor-agent binary is not on PATH."""
        if shutil.which("cursor-agent") is None:
            raise RuntimeError(
                "cursor-agent binary not found on PATH. "
                "Install the Cursor CLI to use the CursorRunner."
            )

    def run(self, payload) -> None:  # type: ignore[override]
        self._check_binary()
        super().run(payload)
