"""Mandatory-agent presence checks, shared by `hivepilot init` and `hivepilot
doctor`.

HivePilot needs at least one coding-agent CLI on PATH to actually run tasks.
The mandatory set is exactly ``claude`` | ``codex`` | ``vibe`` -- ``claude``
is treated as the strongest/most-tested prerequisite (other runners exist,
e.g. the API-only ``openrouter`` agent, but they are not part of this
mandatory set and are not checked here).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

# Type: tuple[str, ...]. Kept without an inline annotation so the invariant
# grep (`MANDATORY_AGENTS\s*=\s*\(?['"]claude['"]`) matches literally.
MANDATORY_AGENTS = ("claude", "codex", "vibe")


@dataclass(frozen=True)
class MandatoryAgentReport:
    """Result of scanning PATH for the mandatory agent CLIs."""

    present: list[str]
    claude_ok: bool
    any_ok: bool


def check_mandatory_agents() -> MandatoryAgentReport:
    """Scan PATH for each of `MANDATORY_AGENTS` via `shutil.which`.

    Returns a `MandatoryAgentReport` with the subset found (`present`,
    preserving `MANDATORY_AGENTS` order), whether `claude` specifically was
    found (`claude_ok`), and whether at least one mandatory agent was found
    at all (`any_ok`).
    """
    present = [name for name in MANDATORY_AGENTS if shutil.which(name)]
    return MandatoryAgentReport(
        present=present,
        claude_ok="claude" in present,
        any_ok=bool(present),
    )
