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

# Canonical set of "agent" runner kinds — the SINGLE source of truth shared by
# hivepilot.registry (active_agent_runner_kinds / _BUILTIN_RUNNERS gating) and
# hivepilot.orchestrator (fail-closed run_pipeline guard). Built-in agent kinds
# + the optional, PATH-gated agent-plugin kinds (gemini/opencode/ollama/pi/
# qwen-code/kimi-cli/antigravity/codex/cursor). Infra runners (shell/terraform/kubectl/…)
# are NOT agents and are deliberately absent. Keep in sync with
# registry._OPTIONAL_AGENT_PLUGIN_KINDS and _BUILTIN_RUNNERS' agent entries —
# do not re-list this literal anywhere else.
#
# `cursor` was added here by the codex-cursor-plugins migration (it was
# previously a hardcoded _BUILTIN_RUNNERS entry but, unlike codex, had never
# been added to this set — a pre-existing gap that meant a pipeline running
# ONLY the `cursor` agent tripped the fail-closed NoAgentRunnerError guard
# even though `cursor` was fully registered and dispatchable; fixed here as
# part of moving it to a gated plugin, alongside every other agent kind).
AGENT_RUNNER_KINDS: frozenset[str] = frozenset(
    {
        "claude",
        "codex",
        "cursor",
        "vibe",
        "openrouter",
        "gemini",
        "opencode",
        "ollama",
        "pi",
        "qwen-code",
        "kimi-cli",
        "antigravity",
    }
)


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
