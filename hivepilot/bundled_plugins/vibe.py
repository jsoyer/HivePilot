"""vibe runner plugin — registers the EXISTING `VibeRunner` behind a gate.

`vibe` was the last third-party agent CLI still hardcoded in
`_BUILTIN_RUNNERS`, beside `claude` and `openrouter`. Those two earn their
place: `claude` is the engine's reference agent, and `openrouter` is API-only
with no binary to gate on. `vibe` is neither -- it is a CLI like `codex`,
`cursor`, `gemini`, `opencode` and `ollama`, all of which were migrated out of
the builtins precisely because a deployment that does not use them should not
carry them.

Same skeleton as `plugins/codex.py`, deliberately: `register()` returns `{}`
when EITHER the enable flag is off or the binary is absent, so a deployment
with `vibe` installed and enabled behaves exactly as it did while it was a
builtin, and one without it stops pretending the kind exists.

`vibe` stays in `AGENT_RUNNER_KINDS` and gains an entry in
`_OPTIONAL_AGENT_PLUGIN_KINDS`, so an unavailable `vibe` resolves to the
actionable `RunnerPluginUnavailableError` -- naming the flag and the binary --
rather than a bare `KeyError`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import VibeRunner

_BINARY = "vibe"
_KIND = "vibe"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `vibe` is on PATH; `degraded` when it isn't — the `vibe`
    kind is simply unavailable in that case (no fallback), matching
    `resolve_runner_class`'s actionable error for anyone routed to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.vibe_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: VibeRunner}, "health": {_KIND: health}}
