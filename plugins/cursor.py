"""cursor runner plugin — registers the EXISTING `CursorRunner` (invocation
logic defined in `hivepilot.runners.cursor_runner`, untouched) under kind
`cursor`, gated on both the per-plugin enable flag AND the `cursor-agent`
CLI binary being on PATH.

Note the binary is `cursor-agent`, NOT `cursor` — matches
`CursorRunner.command_name` and `AGENT_INSTALL_SPECS["cursor"]` in
`hivepilot.services.agent_install`.

codex-cursor-plugins migration moved `codex`/`cursor` OUT of
`hivepilot.registry._BUILTIN_RUNNERS` and into default-on, PATH-gated
plugins exactly like this one — the built-in agent set is now `{claude,
vibe, openrouter}` only. `CursorRunner`'s CLI invocation logic is completely
unchanged; only its *registration* moved here.

See `plugins/gemini.py`'s module docstring for the canonical gated-agent-
plugin skeleton this mirrors exactly: `register()` returns `{}` when EITHER
the flag is off OR the binary is absent, else `{"runners": {"<kind>":
<RunnerClass>}}` — so a config that still references `kind: cursor` keeps
working exactly as before as long as both conditions hold (default:
enabled=True + binary present). When either is false,
`hivepilot.registry.resolve_runner_class` raises the actionable
`RunnerPluginUnavailableError` (naming this exact flag + binary) instead of
a bare `KeyError`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.cursor_runner import CursorRunner

_BINARY = "cursor-agent"
_KIND = "cursor"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `cursor-agent` is on PATH; `degraded` when it isn't — the
    `cursor` kind is simply unavailable in that case (no fallback), matching
    `resolve_runner_class`'s actionable error for anyone routed to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.cursor_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: CursorRunner}, "health": {_KIND: health}}
