from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rich.syntax import Syntax

from hivepilot.console import console


class CommandError(RuntimeError):
    pass



def ensure_command_available(command: str) -> None:
    if shutil.which(command) is None:
        raise CommandError(f"Command not found in PATH: {command}")



def run_command(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 3600,
    dry_run: bool = False,
    show_command: bool = True,
) -> subprocess.CompletedProcess[str]:
    if show_command:
        console.print(Syntax(" ".join(command), "bash", word_wrap=True))

    if dry_run:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=merged_env,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.stdout.strip():
        console.print(result.stdout)
    if result.stderr.strip():
        console.print(result.stderr, style="yellow")
    return result
