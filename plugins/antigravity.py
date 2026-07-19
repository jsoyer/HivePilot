"""antigravity runner plugin — registers `AntigravityRunner` (invocation
logic defined in `hivepilot.runners.prompt_cli_runner`) under kind
`antigravity`, gated on both the per-plugin enable flag AND the `agy` CLI
binary (Google Antigravity CLI) being on PATH.

S3 (follow-on to the runner-defaults-plugins-mode PRD) adds `antigravity` as
a brand-new default-on, PATH-gated agent plugin using the SAME canonical
gated-agent-plugin skeleton established by `plugins/gemini.py` /
`plugins/kimi_cli.py` / `plugins/qwen_code.py`: `register()` returns `{}`
when EITHER the flag is off OR the binary is absent, else
`{"runners": {"<kind>": <RunnerClass>}, "health": {...}}`.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import AntigravityRunner

_BINARY = "agy"
_KIND = "antigravity"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `agy` is on PATH; `degraded` when it isn't — the
    `antigravity` kind is simply unavailable in that case (no fallback),
    matching `resolve_runner_class`'s actionable error for anyone routed
    to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.antigravity_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: AntigravityRunner}, "health": {_KIND: health}}
