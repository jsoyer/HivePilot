from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

from hivepilot.config import Settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep


@dataclass(slots=True)
class RunnerPayload:
    project_name: str
    project: ProjectConfig
    task_name: str
    step: TaskStep
    metadata: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict)


class BaseRunner(Protocol):
    definition: RunnerDefinition
    settings: Settings

    def __init__(self, definition: RunnerDefinition, settings: Settings) -> None: ...

    def run(self, payload: RunnerPayload) -> None: ...


@dataclass(frozen=True, slots=True)
class UsageInfo:
    """Token/cost/actual-model usage captured from a runner's ``capture()``
    call (Phase 24b.2a — opt-in usage capture). All fields are optional and
    None-safe: a runner (or CLI response) that doesn't self-report a given
    field simply leaves it None rather than inventing a value.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None


# ``capture()`` returns only ``str`` (the existing, widely-relied-on contract
# across RunnerRegistry.capture_definition, auditor_service, worker_service,
# and every BaseRunner subclass) — changing that return type would ripple
# through the whole codebase for a feature that's opt-in and off by default.
# Instead a runner that captured usage stashes it here immediately before
# returning its text, and the caller (orchestrator) reads-and-clears it right
# after the call returns via ``pop_last_usage()``.
#
# Thread-safety: ContextVar state is per-thread by default — a new
# ``threading.Thread`` starts with a fresh, empty context, so concurrent step
# execution (e.g. parallel projects) never leaks usage across threads. Within
# a single thread, each ``capture()`` call is synchronous with respect to its
# caller, and the caller always pops immediately after, so there's no
# opportunity for one step's usage to bleed into the next.
_LAST_USAGE: ContextVar[UsageInfo | None] = ContextVar("_LAST_USAGE", default=None)


def set_last_usage(usage: UsageInfo | None) -> None:
    """Stash usage info captured by the most recent runner ``capture()`` call.

    Called by a runner (e.g. ``ClaudeRunner.capture()``) right before
    returning its text, when usage capture is enabled and succeeded.
    """
    _LAST_USAGE.set(usage)


def pop_last_usage() -> UsageInfo | None:
    """Read-and-clear the usage stashed by the last ``capture()`` call.

    Always resets the stash to None so a step that doesn't capture usage
    (flag off, non-claude runner, or a step that raised before reaching the
    stash point) never sees stale data left over from a previous step.
    """
    usage = _LAST_USAGE.get()
    _LAST_USAGE.set(None)
    return usage
