from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    capture_output: bool = False,
):
    logger.debug("shell.run", command=" ".join(command), cwd=str(cwd))
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        check=check,
        text=True,
        capture_output=capture_output,
    )
