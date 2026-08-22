"""claude runner plugin — the last vendor CLI to leave the core.

codex, cursor, gemini, opencode, ollama and vibe already made this trip;
`models.py` records each departure. The rule this finishes: the engine knows
only the vendor-agnostic path — `openrouter` is the one agent kind left in
`_BUILTIN_RUNNERS`.

ONE deliberate difference from the six precedents: FLAG-gated, NOT PATH-gated.
The six are CLI-only, so an absent binary means an unusable kind and `{}` is
honest. claude is not CLI-only: `mode: api` drives Anthropic's Messages API
with `ANTHROPIC_API_KEY` and no binary at all. PATH-gating would delete a kind
that works — the inverse of the langchain lie (#571), and just as wrong. The
binary's absence is `health()`'s to report, naming the api caveat, not the
registry's to erase.
"""

from __future__ import annotations

import shutil
from typing import Any

from hivepilot.plugins import HealthStatus
from hivepilot.runners.claude_runner import ClaudeRunner

_KIND = "claude"


def _binary() -> str:
    """The CONFIGURED command, never a literal: `settings.claude_command` can
    rename the binary, and probing a hardcoded name would report the wrong
    binary's absence."""
    from hivepilot.config import settings

    return settings.claude_command or "claude"


def health(**kwargs: Any) -> HealthStatus:
    binary = _binary()
    if shutil.which(binary):
        return HealthStatus("ok", f"{binary} on PATH")
    return HealthStatus(
        "degraded",
        f"{binary} not on PATH — the CLI path is unavailable; `mode: api` "
        f"steps still work with ANTHROPIC_API_KEY",
    )


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.claude_enabled:
        return {}
    # No PATH check, on purpose — see the module docstring.
    return {"runners": {_KIND: ClaudeRunner}, "health": {_KIND: health}}
