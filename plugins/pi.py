"""pi runner plugin — registers `PiRunner` (invocation logic defined in
`hivepilot.runners.prompt_cli_runner`) under kind `pi`, gated on both the
per-plugin enable flag AND the `pi` CLI binary being on PATH.

Sprint 3 (runner-defaults-plugins-mode PRD) adds `pi`/`qwen-code`/`kimi-cli`
as brand-new default-on, PATH-gated agent plugins using the SAME canonical
gated-agent-plugin skeleton Sprint 2 established (see `plugins/gemini.py`):
`register()` returns `{}` when EITHER the flag is off OR the binary is
absent, else `{"runners": {"<kind>": <RunnerClass>}, "health": {...}}`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import PiRunner

_BINARY = "pi"
_KIND = "pi"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `pi` is on PATH; `degraded` when it isn't — the `pi`
    kind is simply unavailable in that case (no fallback), matching
    `resolve_runner_class`'s actionable error for anyone routed to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.pi_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: PiRunner}, "health": {_KIND: health}}
