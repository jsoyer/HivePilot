"""kimi-cli runner plugin — registers `KimiCliRunner` (invocation logic
defined in `hivepilot.runners.prompt_cli_runner`) under kind `kimi-cli`,
gated on both the per-plugin enable flag AND the `kimi` CLI binary being
on PATH.

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
from hivepilot.runners.prompt_cli_runner import KimiCliRunner

_BINARY = "kimi"
_KIND = "kimi-cli"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `kimi` is on PATH; `degraded` when it isn't — the
    `kimi-cli` kind is simply unavailable in that case (no fallback),
    matching `resolve_runner_class`'s actionable error for anyone routed
    to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.kimi_cli_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: KimiCliRunner}, "health": {_KIND: health}}
