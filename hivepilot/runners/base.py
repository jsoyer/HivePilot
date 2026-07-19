from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from hivepilot.config import Settings
from hivepilot.models import EffectiveLessonsConfig, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.plugins import SkillSpec


@dataclass(slots=True)
class RunnerPayload:
    project_name: str
    project: ProjectConfig
    task_name: str
    step: TaskStep
    metadata: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict)
    # Per-pipeline-lessons-yaml PRD, Sprint 2: the orchestrator's ONE
    # per-run-resolved `EffectiveLessonsConfig` (see
    # `hivepilot.models.resolve_lessons_config`), threaded down so
    # `ClaudeRunner`/`PromptCliRunner` pass it into `knowledge_service.
    # build_lessons_context` instead of that function re-reading the global
    # settings floor directly. `None` (the default) is fully backward
    # compatible -- every call site that predates this Sprint (or any
    # non-pipeline `run_task` call) falls back to floor-only resolution at
    # the consumption site, byte-identical to before this field existed.
    lessons: EffectiveLessonsConfig | None = None


class RunnerModeUnsupportedError(RuntimeError):
    """A resolved pipeline/stage execution mode is not supported by the runner
    it would dispatch to (e.g. ``mode: api`` on a cli-only, non-agent runner).

    Raised by ``validate_runner_mode`` at config-validation / dispatch time —
    BEFORE any subprocess or HTTP call — so an unsupported (kind, mode)
    combination fails closed rather than silently degrading to the wrong path.
    """


class BaseRunner(Protocol):
    definition: RunnerDefinition
    settings: Settings

    # Capability contract (Sprint 1, runner-defaults-plugins-mode PRD): the set
    # of execution modes a runner class can actually honour. Non-agent runners
    # only ever shell out, so the default is cli-only; API-capable agent
    # runners (claude / prompt-cli) override this to ``{"cli", "api"}``. The
    # orchestrator validates the resolved mode against this set before dispatch
    # (see ``validate_runner_mode``). Declared as ``ClassVar`` so ``@dataclass``
    # runner subclasses do NOT treat it as an instance field.
    supported_modes: ClassVar[frozenset[str]] = frozenset({"cli"})

    def __init__(self, definition: RunnerDefinition, settings: Settings) -> None: ...

    def run(self, payload: RunnerPayload) -> None: ...

    # ------------------------------------------------------------------
    # apply_skill — OPTIONAL, structural (Sprint 2, skill-plugin-type PRD)
    # ------------------------------------------------------------------
    # NOT part of this Protocol's required surface (deliberately absent from
    # the signatures above) — exactly like `capture()` (see `registry.py`,
    # `getattr(runner, "capture", None)`) and `is_destructive()` (see
    # `orchestrator.step_requires_approval`, `getattr(runner, "is_destructive",
    # None)`). A runner opts in by defining:
    #
    #   def apply_skill(self, payload: RunnerPayload, skills: list[SkillSpec]) -> RunnerPayload: ...
    #
    # Contract:
    #   * Runner-agnostic: each runner decides *how* a skill is applied to its
    #     own invocation (file materialisation, prompt injection, etc.) — this
    #     Protocol makes no assumption about the mechanism.
    #   * Default-absent == no-op: a runner that does NOT implement
    #     `apply_skill` is never treated as skill-aware; skills are simply
    #     ignored for that runner. Callers MUST use getattr-based discovery
    #     (see `apply_skill_if_supported` below) — never assume the method
    #     exists.
    #   * `applies_to` mismatch: when a skill's optional `applies_to` list is
    #     present and does NOT include this runner's `definition.kind`, the
    #     runner MUST skip that skill (log at info/debug) rather than error —
    #     this is a routing filter, not a validation failure.
    #   * Immutability: MUST return a (new) `RunnerPayload` — the caller's
    #     `payload` must never be mutated in place (mirrors the
    #     immutable-update pattern used throughout the codebase).
    #   * Security: any skill content that may embed `${secret:NAME}`
    #     references MUST be routed through the EXISTING masking /
    #     `${secret:}` resolution choke point (`hivepilot.services.secret_refs
    #     .resolve_secret_refs`, the same one `Orchestrator._resolve_secrets`
    #     uses) before it reaches any sink (materialised file, appended
    #     prompt, log line). See `ClaudeRunner.apply_skill` for the reference
    #     implementation.
    #
    # `SkillSpec` (`hivepilot.plugins`) is imported above purely for this
    # documented signature / for concrete runner implementations to type
    # against — `plugins.py` does not import back from this module, so there
    # is no import cycle.


def apply_skill_if_supported(
    runner: object, payload: RunnerPayload, skills: list[SkillSpec]
) -> RunnerPayload:
    """Structural dispatch helper for the optional `apply_skill` contract
    (see the comment block on `BaseRunner` above).

    Returns *payload* unchanged when *runner* does not implement
    `apply_skill` (the documented no-op default for non-participating
    runners). Otherwise delegates to `runner.apply_skill(payload, skills)`
    and returns its result.

    This is the single reusable choke point future callers (e.g. the
    orchestrator, Sprint 4) should use rather than each re-implementing the
    same `getattr(..., None)` check.
    """
    apply_skill = getattr(runner, "apply_skill", None)
    if apply_skill is None:
        return payload
    return apply_skill(payload, skills)


def resolve_runner_effort(definition: RunnerDefinition, step: TaskStep) -> str | None:
    """Resolve the effective reasoning-effort level for a step — the SINGLE
    resolution shared by every effort-aware runner (Claude, Codex).

    The runner definition's ``effort`` is AUTHORITATIVE: it already carries the
    orchestrator's ``policy > stage > role > runner-default`` precedence result
    (see ``hivepilot.roles.resolve_stage_dispatch``). A per-step
    ``TaskStep.effort`` applies only as a FALLBACK when nothing was resolved
    upstream (``definition.effort is None``) — so a step can never silently
    override a stage- or policy-mandated effort (policy stays the top control).
    Returns ``None`` when neither is set.

    Generic, runner-agnostic accessor: a runner with no effort concept (most
    CLIs) can safely ignore the return value entirely — this helper never
    raises and never invents a value.
    """
    return definition.effort if definition.effort is not None else step.effort


def validate_runner_mode(kind: str, supported_modes: frozenset[str], mode: str) -> None:
    """Fail closed when *mode* is not in *supported_modes* for runner *kind*.

    Called by the orchestrator once the effective mode is resolved and the
    concrete runner class is known, BEFORE the step actually runs — so a
    ``mode: api`` step targeting a cli-only runner never spawns a subprocess or
    issues an HTTP request. The message names the kind, the offending mode, and
    the supported set so the operator can fix the config immediately.
    """
    if mode not in supported_modes:
        raise RunnerModeUnsupportedError(
            f"'{kind}' does not support mode '{mode}' (supported: {sorted(supported_modes)})"
        )


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
