"""grok runner plugin — xAI's `grok` CLI as a default-on, PATH-gated kind.

Same shape as `codex.py` and the five others that moved out of builtin
registration: the kind exists only when the binary does, so an operator routed
to it gets `resolve_runner_class`'s actionable error BEFORE a run row exists,
rather than a failure mid-dispatch. A builtin declares availability by its
presence in a dict; a plugin declares it by a check.

`GrokRunner` inherits `ClaudeRunner` — see that module for why a
`PromptCliRunner` subclass would have silently dropped every role's
`permission_mode` and `allowed_tools`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.grok_runner import GrokRunner

_BINARY = "grok"
_KIND = "grok"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `grok` is on PATH, `degraded` when it is not — the kind is
    then simply unavailable, with no fallback.

    Worth knowing when this reports `degraded` on a box where the binary is
    installed: grok's installer is PER-USER (`$HOME/.grok/bin`, no sudo), and
    the systemd units set no `PATH`, so they inherit systemd's default which
    does not include it. `shutil.which` is right and the install is what needs
    fixing — symlink into a directory the SERVICE's PATH contains.
    """
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.grok_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: GrokRunner}, "health": {_KIND: health}}
