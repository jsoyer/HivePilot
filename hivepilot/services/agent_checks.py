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
# Read as "at least ONE of these", which is what the only consumer says
# (`doctor`: "HivePilot needs at least one of: ..."), not "all of these are
# required". `codex` and `vibe` becoming gated plugins (#234, #520) therefore
# does NOT remove them here: an install carrying only codex does have a
# dispatchable agent, and shrinking this tuple would tell that operator they
# have none. `claude` stays first because it is the primary prerequisite and
# `claude_ok` is reported separately.
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


# Agent kinds that reach their model over HTTP and have NO CLI binary, ever.
#
# Canonical here rather than as a private local in `cli.py`'s table, which is
# where it used to live. A second consumer -- `doctor_liveness`'s agent CLI
# version check -- could not see it and reported `openrouter` as "registered
# as a runner but its CLI is not on PATH", advising the operator to install a
# binary that does not exist OR to disable a runner that works. A cleanup
# suggestion that breaks a working feature is worse than no suggestion.
#
# Membership is a property of the RUNNER, not of a deployment: OpenRouterRunner
# is documented API-only at hivepilot/registry.py and hivepilot/models.py.
API_ONLY_AGENT_KINDS: frozenset[str] = frozenset({"openrouter"})


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
