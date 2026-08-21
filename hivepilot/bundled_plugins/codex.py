"""codex runner plugin — registers the EXISTING `CodexRunner` (invocation
logic defined in `hivepilot.runners.prompt_cli_runner`, untouched) under
kind `codex`, gated on both the per-plugin enable flag AND the `codex` CLI
binary being on PATH.

codex-cursor-plugins migration moved `codex`/`cursor` OUT of
`hivepilot.registry._BUILTIN_RUNNERS` and into default-on, PATH-gated
plugins exactly like this one — the built-in agent set is now `{claude,
vibe, openrouter}` only. `CodexRunner`'s CLI invocation logic is completely
unchanged; only its *registration* moved here.

See `plugins/gemini.py`'s module docstring for the canonical gated-agent-
plugin skeleton this mirrors exactly: `register()` returns `{}` when EITHER
the flag is off OR the binary is absent, else `{"runners": {"<kind>":
<RunnerClass>}}` — so a config that still references `kind: codex` keeps
working exactly as before as long as both conditions hold (default:
enabled=True + binary present). When either is false,
`hivepilot.registry.resolve_runner_class` raises the actionable
`RunnerPluginUnavailableError` (naming this exact flag + binary) instead of
a bare `KeyError`.

`codex` also stays in `hivepilot.services.agent_checks.MANDATORY_AGENTS` —
`check_mandatory_agents()` scans PATH directly (`shutil.which("codex")`),
unaffected by whether `codex` is currently registered as builtin or plugin.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import CodexRunner

_BINARY = "codex"
_KIND = "codex"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `codex` is on PATH; `degraded` when it isn't — the `codex`
    kind is simply unavailable in that case (no fallback), matching
    `resolve_runner_class`'s actionable error for anyone routed to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.codex_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: CodexRunner}, "health": {_KIND: health}}
