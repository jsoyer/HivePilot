"""ollama runner plugin — registers the EXISTING `OllamaRunner` (invocation
logic defined in `hivepilot.runners.prompt_cli_runner`, untouched) under kind
`ollama`, gated on both the per-plugin enable flag AND the `ollama` CLI
binary being on PATH.

See `plugins/gemini.py`'s module docstring for the full rationale — this is
the same canonical gated-agent-plugin skeleton, applied to `ollama`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import OllamaRunner

_BINARY = "ollama"
_KIND = "ollama"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `ollama` is on PATH; `degraded` when it isn't — the `ollama`
    kind is simply unavailable in that case (no fallback), matching
    `resolve_runner_class`'s actionable error for anyone routed to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.ollama_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: OllamaRunner}, "health": {_KIND: health}}
