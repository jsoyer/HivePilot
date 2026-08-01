from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
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


class RunnerExecutionError(RuntimeError):
    """A runner's underlying subprocess/API call failed (non-zero exit, or an
    equivalent execution failure) -- raised in place of a bare ``RuntimeError``
    so the failure carries STRUCTURED diagnostic context alongside its human
    ``str()`` message (explicit-failure-logs sprint, Part A.1).

    ``context`` is a flat ``dict`` of machine-readable fields (e.g.
    ``exit_code``, ``runner_kind``, ``model``, ``role``, ``task``, ``stage``,
    ``project``, ``permission_mode``, ``skill_applied``, ``stderr_excerpt``,
    and an optional ``hint`` for a recognised failure signature) a caller can
    merge straight into a structured log call (see
    ``Orchestrator``'s ``run.failure`` handler) instead of only logging the
    opaque exception text. Any value already redacted by the raiser (see
    ``ClaudeRunner._runner_failure_context``) -- this class does no
    redaction of its own, it only carries what it's given.

    ``str(exc)`` stays a plain, human-readable message (unchanged shape from
    the plain ``RuntimeError`` this replaces) so every existing caller that
    only reads/matches the exception text keeps working unmodified.
    """

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = context or {}


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


def prompt_file_not_found_message(
    payload: RunnerPayload, settings_obj: Settings, prompt_file: str, prompt_path: Path
) -> str:
    """Build the ``FileNotFoundError`` message for a step's unresolved
    ``prompt_file`` -- names the referencing task/step and lists every
    directory ``Settings.resolve_config_path`` searched, instead of only
    printing the final (possibly nonsensical) resolved path.

    Real incident: an operator's ``tasks.yaml`` referenced a
    ``prompt_file`` that did not exist anywhere on the box. The service
    ran with ``cwd=/``, so resolution fell through to the base_dir tier
    and the ORIGINAL message read ``Prompt file not found:
    /security_review.md`` -- it named neither the failing task/step nor
    any of the other directories that were also checked, so the operator
    had nothing actionable beyond a nonsense absolute path.

    Shared by ``ClaudeRunner._assemble_prompt`` and ``PromptCliRunner.
    _load_prompt`` so both runners raise an identically actionable error
    instead of drifting into two different wordings. Uses ``Settings.
    config_path_search_dirs()`` (the exact tier list ``resolve_config_path``
    consults) rather than reimplementing the search order.
    """
    searched = ", ".join(str(d) for d in settings_obj.config_path_search_dirs())
    return (
        f"Prompt file not found for task '{payload.task_name}' step "
        f"'{payload.step.name}' (prompt_file={prompt_file!r}); resolved to "
        f"{prompt_path} -- searched: {searched}"
    )


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

    ``cache_read_tokens``/``cache_creation_tokens`` (usage-capture-modelusage
    fix) are prompt-caching token counts -- ``input_tokens`` alone dramatically
    UNDERCOUNTS real volume once caching is in play (a cache hit means the
    real prompt bytes show up here, not in ``input_tokens``), and these two
    categories are billed at DIFFERENT rates from base input/output tokens
    and from each other, so they are tracked as distinct fields rather than
    folded into ``input_tokens``.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


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


# Live incident (explicit-failure-logs follow-up): a `developer` stage ran
# headless with `--permission-mode acceptEdits`, the agent replied "I need
# your approval to run shell commands in this session... Should I proceed?",
# wrote NO files, and `claude` still exited 0 -- HivePilot recorded the run
# as a SUCCESS, the pipeline advanced to Review, and no code/PR was ever
# produced. Exit 0 != work done. Each phrase below is a clear signature of
# the AGENT ITSELF asking for permission/approval rather than acting -- never
# ordinary prose that merely mentions the word "approval" (see
# `detect_noop_permission_response`'s docstring for the conservative-
# matching rationale; matched by test_base.py's
# `test_does_not_false_positive_on_approval_workflow_prose`).
_NOOP_PERMISSION_PATTERNS: tuple[str, ...] = (
    "i need your approval",
    "need permission to",
    # "requires your approval" (first person), NOT "requires approval":
    # the bare third-person form is ordinary governance prose that a
    # reviewing role writes legitimately ("this PR requires approval from
    # the release manager"), and matching it failed real, completed CTO and
    # groomer reviews. Every other phrase here is unambiguously the agent
    # speaking about itself; this one was not, which broke the conservative
    # contract stated above.
    "requires your approval",
    "cannot be used with root",
    "should i proceed",
    "permission grant",
    "i don't have permission",
)


def detect_noop_permission_response(text: str | None) -> str | None:
    """Detect a captured agent response that is a NO-OP permission/approval
    REQUEST rather than completed work, and return a human-readable reason,
    or ``None`` when *text* looks like ordinary (possibly completed-work)
    output.

    Deliberately CONSERVATIVE -- plain, case-insensitive substring matches
    against a short list of phrasings that only ever occur when the agent
    itself is asking for permission/approval instead of proceeding. A
    document that merely *discusses* an approval workflow (e.g. "see the
    approval workflow spec") does not contain any of these phrasings and
    must never match -- a false negative here just means the operator falls
    back to noticing the empty diff themselves; a false positive would
    incorrectly fail a step that actually did the work, which is worse.

    Returns ``None`` for ``None``/empty *text* -- nothing to inspect.
    """
    if not text:
        return None
    lowered = text.lower()
    for pattern in _NOOP_PERMISSION_PATTERNS:
        index = lowered.find(pattern)
        if index != -1:
            # Quote what actually matched. Without this the guard REPLACES the
            # agent's response with its own verdict, so nothing downstream can
            # tell a true positive from a false one -- an operator debugging a
            # failed step has literally no evidence to look at. Observed on a
            # real CTO review: the step was failed and the output discarded,
            # leaving no way to know whether the agent had asked for
            # permission or simply written the words in a report.
            start = max(0, index - 80)
            excerpt = text[start : index + len(pattern) + 80].strip().replace("\n", " ")
            return (
                "agent response reads like a permission/approval request, not "
                f"completed work (matched phrase: {pattern!r}; context: ...{excerpt}...)"
            )
    return None


# Explicit-failure-logs follow-up (Bug 1, run 243 live incident): a runner
# subprocess exit code, `-N` for signal `N` per the POSIX/`subprocess`
# convention (see `subprocess.CalledProcessError.returncode`,
# `os.WIFSIGNALED`), means the OS killed the process -- it never ran to
# completion, so nothing about "the tests" or "the agent's logic" can be
# blamed. A run that recorded `-9` (SIGKILL, almost always the kernel
# OOM-killer) as `test_failure` sends the operator debugging the wrong
# thing entirely. This is distinct from the command actually running and
# reporting ITS OWN failure via a positive exit code (real test failure /
# bad agent logic) -- that classification must never change.
@dataclass(frozen=True, slots=True)
class SignalDeath:
    """Classification of a runner subprocess killed by a POSIX signal.

    ``deliberate`` is ``True`` only for SIGTERM: an intentional stop/cancel
    request (e.g. systemd/container shutdown, a manual `kill`), never a
    crash -- callers must not report it as one. Every other signal
    (SIGKILL, SIGABRT, SIGSEGV, ...) is a genuine, non-deliberate
    INFRASTRUCTURE failure. ``message`` is an actionable, operator-facing
    string in the WHAT/WHY/FIX spirit of ``hivepilot.services.config_doctor``
    findings.
    """

    signal_number: int
    signal_name: str
    deliberate: bool
    message: str


def classify_signal_exit(exit_code: int | None) -> SignalDeath | None:
    """Classify *exit_code* as a POSIX signal death, or return ``None`` when
    it is not one.

    Returns ``None`` for ``exit_code is None`` or ``exit_code >= 0`` -- the
    command actually ran and either succeeded or reported ITS OWN, genuine
    failure via a positive exit code (a real test failure, bad agent logic,
    a CLI usage error, ...). Only a NEGATIVE exit code (per
    ``subprocess.CalledProcessError.returncode``'s ``-N`` == "killed by
    signal N" convention) is ever classified here -- callers must never
    reclassify a positive exit code based on this function.

    Never raises: an unrecognised signal number (rare, platform-specific)
    still yields a ``SignalDeath`` with a synthesised ``SIG<N>`` name rather
    than crashing the classifier itself while it is handling an already
    unusual failure.
    """
    if exit_code is None or exit_code >= 0:
        return None
    import signal as _signal

    signum = -exit_code
    try:
        signal_name = _signal.Signals(signum).name
    except ValueError:
        signal_name = f"SIG{signum}"

    if signum == _signal.SIGTERM:
        return SignalDeath(
            signal_number=signum,
            signal_name=signal_name,
            deliberate=True,
            message=(
                f"the runner process was terminated by {signal_name} — a deliberate "
                "stop/cancel request (e.g. a service restart, a container shutdown, "
                "or a manual `kill`), never a test/logic failure and never treated "
                "as an unexpected process failure."
            ),
        )

    if signum == _signal.SIGKILL:
        return SignalDeath(
            signal_number=signum,
            signal_name=signal_name,
            deliberate=False,
            message=(
                f"the runner process was killed by {signal_name} — the operating "
                "system killed it, almost always the OOM (out-of-memory) killer. "
                "This is an INFRASTRUCTURE failure, not a test/logic failure: the "
                "command never ran to completion, so nothing about the tests or "
                "the agent's own logic can be blamed (this is also why stderr is "
                "typically empty -- the process never got to write anything). "
                "why: an LXC/container with an unlimited memory.max can still be "
                "OOM-killed by a contended HOST kernel — check memory AVAILABLE "
                "(not total, not `free`'s cached/buffered figure) on the host at "
                "the time of the run, e.g. `free -m`'s 'available' column or the "
                "cgroup's `memory.current` vs `memory.max`. "
                "fix: raise the container's memory limit/reservation, reduce "
                "concurrent runner load, or move this task to a host with more "
                "available memory."
            ),
        )

    return SignalDeath(
        signal_number=signum,
        signal_name=signal_name,
        deliberate=False,
        message=(
            f"the runner process crashed with {signal_name} (signal {signum}) — an "
            "INFRASTRUCTURE failure (the OS terminated it), not a test/logic failure."
        ),
    )
