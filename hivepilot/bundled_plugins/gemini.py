"""gemini runner plugin — registers the EXISTING `GeminiRunner` (invocation
logic defined in `hivepilot.runners.prompt_cli_runner`, untouched) under kind
`gemini`, gated on both the per-plugin enable flag AND the `gemini` CLI
binary being on PATH.

Sprint 2 (runner-defaults-plugins-mode PRD) moved `gemini`/`opencode`/
`ollama` OUT of `hivepilot.registry._BUILTIN_RUNNERS` and into default-on,
PATH-gated plugins exactly like this one. The codex-cursor-plugins migration
later did the same for `codex`/`cursor` — the built-in agent set is now
`{claude, vibe, openrouter}` only. `GeminiRunner`'s CLI invocation logic is
completely unchanged; only its *registration* moved here.

This is the CANONICAL gated-agent-plugin skeleton (`plugins/opencode.py` /
`plugins/ollama.py` mirror it exactly; Sprint 3's new plugins copy it too):
`register()` returns `{}` when EITHER the flag is off OR the binary is
absent, else `{"runners": {"<kind>": <RunnerClass>}}` — so a config that
still references `kind: gemini` keeps working exactly as before as long as
both conditions hold (default: enabled=True + binary present). When either
is false, `hivepilot.registry.resolve_runner_class` raises the actionable
`RunnerPluginUnavailableError` (naming this exact flag + binary) instead of
a bare `KeyError`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import GeminiRunner

_BINARY = "gemini"
_KIND = "gemini"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `gemini` is on PATH; `degraded` when it isn't — the `gemini`
    kind is simply unavailable in that case (no fallback), matching
    `resolve_runner_class`'s actionable error for anyone routed to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.gemini_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: GeminiRunner}, "health": {_KIND: health}}
