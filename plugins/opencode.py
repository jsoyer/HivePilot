"""opencode runner plugin — registers the EXISTING `OpenCodeRunner`
(invocation logic defined in `hivepilot.runners.prompt_cli_runner`,
untouched) under kind `opencode`, gated on both the per-plugin enable flag
AND the `opencode` CLI binary being on PATH.

See `plugins/gemini.py`'s module docstring for the full rationale — this is
the same canonical gated-agent-plugin skeleton, applied to `opencode`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import OpenCodeRunner

_BINARY = "opencode"
_KIND = "opencode"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `opencode` is on PATH; `degraded` when it isn't — the
    `opencode` kind is simply unavailable in that case (no fallback),
    matching `resolve_runner_class`'s actionable error for anyone routed to
    it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.opencode_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: OpenCodeRunner}, "health": {_KIND: health}}
