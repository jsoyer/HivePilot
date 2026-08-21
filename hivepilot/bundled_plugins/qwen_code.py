"""qwen-code runner plugin — registers `QwenCodeRunner` (invocation logic
defined in `hivepilot.runners.prompt_cli_runner`) under kind `qwen-code`,
gated on both the per-plugin enable flag AND the `qwen` CLI binary being
on PATH.

Sprint 3 (runner-defaults-plugins-mode PRD) adds `pi`/`qwen-code`/`kimi-cli`
as brand-new default-on, PATH-gated agent plugins using the SAME canonical
gated-agent-plugin skeleton Sprint 2 established (see `plugins/gemini.py`):
`register()` returns `{}` when EITHER the flag is off OR the binary is
absent, else `{"runners": {"<kind>": <RunnerClass>}, "health": {...}}`.

Note the CLI binary is `qwen`, but the registered kind is `qwen-code` (the
package/CLI's project name) — mirrors how `ollama`'s CLI binary/kind pair
already diverges from a strict 1:1 naming.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.prompt_cli_runner import QwenCodeRunner

_BINARY = "qwen"
_KIND = "qwen-code"


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `qwen` is on PATH; `degraded` when it isn't — the
    `qwen-code` kind is simply unavailable in that case (no fallback),
    matching `resolve_runner_class`'s actionable error for anyone routed
    to it."""
    if shutil.which(_BINARY):
        return HealthStatus("ok", f"{_BINARY} on PATH")
    return HealthStatus("degraded", f"{_BINARY} not on PATH — kind '{_KIND}' unavailable")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.qwen_code_enabled:
        return {}
    if shutil.which(_BINARY) is None:
        return {}
    return {"runners": {_KIND: QwenCodeRunner}, "health": {_KIND: health}}
