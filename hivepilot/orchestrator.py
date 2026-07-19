from __future__ import annotations

import concurrent.futures
import json
import math
import subprocess
import threading
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import questionary

from hivepilot.config import settings
from hivepilot.models import (
    EffectiveDebateConfig,
    EffectiveLessonsConfig,
    EffortLevel,
    Group,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskConfig,
    TaskStep,
    resolve_debate_config,
    resolve_effort,
    resolve_lessons_config,
    resolve_mode,
    resolve_stage_model,
)

try:
    from hivepilot.services import metrics as _metrics  # noqa: F401

    _METRICS_AVAILABLE = True
except ImportError:
    _metrics = None  # type: ignore[assignment]
    _METRICS_AVAILABLE = False
from hivepilot.observability.tracing import (
    current_context,
    get_tracer,
    record_exception_on_span,
    use_context,
)
from hivepilot.pipelines import write_stage_artifact
from hivepilot.plugins import PluginManager, SkillSpec
from hivepilot.registry import RunnerRegistry
from hivepilot.runners.base import (
    RunnerPayload,
    UsageInfo,
    apply_skill_if_supported,
    pop_last_usage,
    validate_runner_mode,
)
from hivepilot.services import (
    async_run_service,
    knowledge_service,
    notification_service,
    policy_service,
    scan_service,
    state_service,
)
from hivepilot.services.agent_report import parse_agent_report, parse_agent_requests
from hivepilot.services.artifact_service import ArtifactManager
from hivepilot.services.config_provenance import redact_text, redact_value, register_secret_value
from hivepilot.services.git_service import isolated_worktree, perform_git_actions
from hivepilot.services.interaction_service import (
    Interaction,
    InteractionService,
    log_challenge_interaction,
    log_request_interaction,
)
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines, load_projects, load_tasks
from hivepilot.services.secret_refs import resolve_secret_refs
from hivepilot.services.secrets_service import secret_resolver
from hivepilot.services.state_service import RunStatus
from hivepilot.utils.io import create_run_directory, write_summary
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from hivepilot.roles import Role
    from hivepilot.services import lessons_service
    from hivepilot.services.debate_service import Position

logger = get_logger(__name__)

_DETAILS_MAX_BULLETS = 6
_DETAILS_FALLBACK_CHARS = 600


def _resolve_role_from_display(display_name: str) -> str | None:
    """Resolve a role key from a display string like "Aliénor (CEO)".

    Searches the ROLES registry for a role whose ``display_name`` or ``title``
    matches *display_name* (case-insensitive, partial/substring match).
    Returns the role key (e.g. ``"ceo"``) or ``None`` if nothing matches.
    """
    from hivepilot.roles import ROLES

    needle = display_name.strip().lower()
    for role_key, role in ROLES.items():
        candidates = []
        if role.display_name:
            candidates.append(role.display_name.lower())
        if role.title:
            candidates.append(role.title.lower())
        # Also check the formatted "display_name (title)" pattern
        if role.display_name and role.title:
            candidates.append(f"{role.display_name.lower()} ({role.title.lower()})")
        for candidate in candidates:
            if candidate in needle or needle in candidate:
                return role_key
    return None


def _build_checkpoint_details(
    prior_chunks: list[str],
    completed: list[str],
    next_stage: str,
    components: list[str],
    group_mode: bool,
) -> str:
    """Build a human-readable details string for the Telegram approval DM.

    Pure function — no I/O, no side-effects; safe to unit-test in isolation.

    The resulting string is composed of (in order):
    - Components line (group mode only)
    - Completed / next stage lines
    - Plan summary extracted from the last prior_chunk via parse_agent_report;
      falls back to a plain text excerpt when structured parsing yields nothing.
    - A footer pointing to the Obsidian vault.
    """
    lines: list[str] = []

    if group_mode and components:
        lines.append(f"🎯 *Components:* {', '.join(components)}")

    if completed:
        lines.append(f"✅ *Completed:* {', '.join(completed)}")
    lines.append(f"▶️ *Next:* {next_stage}")

    last_chunk = prior_chunks[-1].strip() if prior_chunks else ""
    if last_chunk:
        report = parse_agent_report(last_chunk)
        bullets = report.summary[:_DETAILS_MAX_BULLETS]
        if bullets:
            lines.append("\n📋 *Plan summary:*")
            for bullet in bullets:
                lines.append(f"  • {bullet}")
        else:
            # Fallback: plain excerpt of the last chunk
            excerpt = last_chunk[:_DETAILS_FALLBACK_CHARS]
            if len(last_chunk) > _DETAILS_FALLBACK_CHARS:
                excerpt += "…"
            lines.append(f"\n📋 *Plan excerpt:*\n{excerpt}")

    lines.append("\n📂 _Full plan: in the Obsidian vault._")

    return "\n".join(lines)


class StepApprovalPending(Exception):  # noqa: N818 — mirrors QuotaDeferredError naming
    """Raised inside `_execute_task` when a step-level destructive-operation
    approval gate pauses a task mid-execution (see `step_requires_approval`).

    Mirrors how `QuotaDeferredError` interrupts `_execute_task` for a
    non-failure reason: by the time this is raised, the run has already been
    marked `RunStatus.PAUSED` and an approval request has already been
    recorded — callers (``_run_task_body``'s ThreadPoolExecutor result
    handling, ``run_approved``) must catch it distinctly and must NOT treat
    it as a step/task failure (no `record_step(..., "failed", ...)`, no
    `complete_run(..., "failed", ...)`).
    """


class RunCancelled(Exception):  # noqa: N818 — mirrors StepApprovalPending/QuotaDeferredError naming
    """Raised inside `_execute_task_body`'s step loop when cooperative
    cancellation was requested for `run_id` (Mirador actionable dashboard
    PRD, Sprint 4 -- `POST /v1/runs/{run_id}/cancel` ->
    `async_run_service.request_cancel` -> checked via
    `async_run_service.is_cancel_requested` at the top of each step
    boundary).

    Mirrors `StepApprovalPending`: by the time this is raised,
    `state_service.complete_run(run_id, RunStatus.CANCELLED.value, ...)` has
    ALREADY been called -- callers (`_run_async_task`) must catch it
    distinctly and must NOT call `complete_run` again (no double-terminal-
    status write, no overwriting CANCELLED with a later "success"/"failed").

    Only ever raised on the async (`POST /v1/runs`) path: `run_id` is
    threaded in there, and only `async_run_service.submit_run` populates the
    in-flight cancellation registry `is_cancel_requested` checks against --
    sync `run_task`/`_run_task_body` callers never register a run there, so
    cancellation can never trigger for them (documented, not a bug: there is
    no equivalent "stop" affordance for a synchronous run that already holds
    the HTTP request open).
    """


def _resolve_runner_for_destructive_check(runner_def: RunnerDefinition) -> object | None:
    """Best-effort instantiate the runner class for *runner_def* purely to
    query the optional `is_destructive()` method (see `step_requires_approval`
    below) — never to execute anything.

    Resolves via `hivepilot.registry.resolve_runner_class`, the same
    kind->class lookup `RunnerRegistry.execute_definition`/
    `capture_definition` use, so the gate checks the exact runner class that
    would actually run this step. Returns None when the kind can't be
    resolved (unknown/mocked kind — common in unit tests that stub
    `Orchestrator.registry` wholesale); callers treat that identically to "no
    `is_destructive` method", i.e. not destructive, matching the documented
    default for runners that don't implement it.
    """
    from hivepilot.registry import resolve_runner_class

    try:
        runner_cls = resolve_runner_class(runner_def.kind)
        return runner_cls(runner_def, settings)
    except Exception:  # noqa: BLE001 — any resolution failure -> "can't tell", not destructive
        return None


def step_requires_approval(runner: object, step: TaskStep, payload: RunnerPayload) -> bool:
    """Return True iff *step* must pause for human approval before it runs.

    Extends the existing per-task (`policy.require_approval`) and per-stage
    (`PipelineStage.pause_before`) approval mechanisms one level finer, to a
    single step: a step needs approval iff ``step.require_approval`` is True
    **or** the runner declares its currently-resolved operation destructive
    via an OPTIONAL, structural (getattr-discovered — like ``capture()``)
    ``is_destructive(payload) -> bool`` method. A runner without that method
    is never treated as destructive (explicit opt-in per runner).

    Fail-closed: if ``is_destructive`` itself raises, the step is treated as
    destructive rather than silently letting a potentially-destructive
    operation through ungated.
    """
    if step.require_approval:
        return True
    is_destructive = getattr(runner, "is_destructive", None) if runner is not None else None
    if is_destructive is None:
        return False
    try:
        return bool(is_destructive(payload))
    except Exception:  # noqa: BLE001 — fail-closed: an error classifying == treat as destructive
        return True


def _find_gating_step(
    task: TaskConfig,
    policy: policy_service.Policy | None,
    registry: RunnerRegistry,
) -> TaskStep | None:
    """Return the first step in *task* that would trip `step_requires_approval`
    — statically, without executing anything — or None if no step would.

    Used at TASK START (before entering `isolated_worktree`) to fail-closed on
    the combination of git-worktree isolation with a step-level approval gate:
    a mid-task `StepApprovalPending` pause unwinds through the worktree
    context, whose `finally` unconditionally runs `git worktree remove
    --force` (see `hivepilot.services.git_service.isolated_worktree`),
    deleting any prior steps' file edits before a resume could ever see them.
    Detecting this up front lets `_execute_task` refuse instead of silently
    losing that work.

    This is deterministic: `TaskStep.require_approval` is static, and
    `is_destructive(payload)` (see the runners in `hivepilot/runners/`) reads
    only step/definition data (e.g. `payload.step.command`), never runtime
    step output — so checking every step before any of them run is safe and
    agrees with what `step_requires_approval` would decide once the step
    actually executes. Mirrors the two runner-resolution branches in
    `_execute_task`'s step loop (role-based vs. explicit-runner steps). A step
    whose runner kind can't be resolved is treated as non-gating — the same
    fail-open-on-resolution-failure default `_resolve_runner_for_destructive_check`
    already uses (an unresolvable runner can't be destructive by definition).
    """
    _role_runner_def: RunnerDefinition | None = None
    if task.role:
        from typing import cast

        from hivepilot.models import RunnerKind
        from hivepilot.roles import get_role, resolve_host, resolve_stage_dispatch

        try:
            # No pipeline stage is available at this static, pre-execution
            # probe (called once per task start, before any stage context
            # exists) — resolves identically to `resolve_runner` (stage_model/
            # stage_effort default None).
            runner_kind, role_model, _role_effort = resolve_stage_dispatch(task.role, policy)
            role_options: dict[str, str] = {}
            role_perm = get_role(task.role).permission_mode
            if role_perm:
                role_options["permission_mode"] = role_perm
            _role_runner_def = RunnerDefinition(
                name=f"role:{task.role}",
                kind=cast(RunnerKind, runner_kind),
                command=None,
                model=role_model,
                effort=_role_effort,
                host=resolve_host(task.role, policy),
                options=role_options,
            )
        except Exception:  # noqa: BLE001 — can't resolve role runner: don't gate
            _role_runner_def = None

    _probe_project = ProjectConfig(path=Path("."))
    for step in task.steps:
        if step.require_approval:
            return step
        if task.role:
            runner_def = _role_runner_def
        else:
            try:
                runner_key = step.runner_ref or step.runner
                runner_def = registry._definition_for(runner_key)
            except Exception:  # noqa: BLE001 — unresolvable step runner: don't gate
                runner_def = None
        if runner_def is None:
            continue
        gate_runner = _resolve_runner_for_destructive_check(runner_def)
        probe_payload = RunnerPayload(
            project_name="",
            project=_probe_project,
            task_name="",
            step=step,
            metadata={},
            secrets={},
        )
        if step_requires_approval(gate_runner, step, probe_payload):
            return step
    return None


def _runner_for_stage(stage: PipelineStage) -> str:
    """Return the runner name for a pipeline stage.

    Currently always returns ``"claude"`` (Claude-first seam).  Future sprints
    may inspect *stage* fields (e.g. a ``runner`` override) to route to other
    runners.
    """
    return "claude"


def _resolve_effective_mode(step: TaskStep, stage_mode: str | None) -> str:
    """Resolve the pipeline/stage-driven execution mode for a step.

    Precedence: an explicit per-step ``metadata['mode']`` wins over the
    pipeline/stage-resolved ``stage_mode`` (from ``resolve_mode``), which falls
    back to ``"cli"``. The orchestrator deliberately does NOT consult the runner
    definition's ``options['mode']`` here: that channel is the runner's OWN
    fallback (``step.metadata > options > "cli"`` inside each runner), so a
    plain ``run_task`` (``stage_mode`` None, no step-metadata mode) resolves to
    ``"cli"`` and the orchestrator injects nothing — leaving the runner's
    existing ``options['mode']`` behaviour byte-identical.
    """
    return (step.metadata.get("mode") or stage_mode or "cli").lower()


def _resolve_step_provider_model(
    runner_def: RunnerDefinition, step: TaskStep
) -> tuple[str | None, str | None]:
    """Resolve the ``(provider, model)`` pair to persist for a step's
    ``state_service.record_step`` call, from the ``RunnerDefinition`` that
    actually executed (or was attempted for) it (Phase 24b.1 — persist
    provider/model per step; see ``Plans/phase24-analytics-api-spec.md``
    Sprint 24b step 3).

    - For a prompt-CLI runner configured in API mode
      (``options.mode == "api"``; see ``PromptCliRunner._run_api``), the
      *real* provider is ``options.api_provider`` (e.g. ``"openai"``,
      ``"anthropic"``) rather than the runner kind (e.g. ``"codex"``), and
      the model mirrors ``PromptCliRunner._run_api``'s own resolution:
      ``step.metadata["model"]`` else ``options.api_model``.
    - Otherwise the provider is the runner **kind** (e.g. ``"claude"``,
      ``"shell"``, ``"cursor"``) and the model mirrors
      ``PromptCliRunner._build_cli_args``'s resolution:
      ``step.metadata["model"]`` else ``runner_def.model``.

    Only what's genuinely known at the orchestrator level is returned — no
    invented values. In particular this does NOT replicate
    ``ClaudeRunner._resolve_model``'s deeper profile-based lookup or its
    ``settings.default_model`` fallback (both live inside the runner and are
    out of scope for this "safe first step" sprint) — a Claude step whose
    model comes from a profile or the global default therefore persists
    whatever ``runner_def.model`` already carries (often ``None`` in that
    case), not the runner's fully-resolved model string.
    """
    options = runner_def.options or {}
    if options.get("mode") == "api":
        provider = options.get("api_provider")
        model = step.metadata.get("model") or options.get("api_model")
        return provider, model
    model = step.metadata.get("model") or runner_def.model
    return runner_def.kind, model


def _record_step_success(
    run_id: int,
    step_name: str,
    provider: str | None,
    model: str | None,
    usage: UsageInfo | None,
) -> None:
    """Call ``state_service.record_step`` for a successful step, threading
    captured usage (Phase 24b.2a — opt-in usage capture) when present.

    When *usage* is None (flag off, non-claude runner, or nothing captured),
    this issues the EXACT same ``record_step`` call as before this sprint —
    Phase 24b.1 callers/tests stay byte-compatible. When *usage* carries an
    actual model (from the JSON envelope), it overrides *model* — this closes
    the 24b.1 gap where profile/default-model claude steps persisted None.
    """
    if usage is None:
        state_service.record_step(run_id, step_name, "success", provider=provider, model=model)
        return
    state_service.record_step(
        run_id,
        step_name,
        "success",
        provider=provider,
        model=usage.model or model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
    )


def _parse_brain(entry: str, default_runner: str) -> tuple[str, str]:
    """Split a debate brain spec into ``(runner, model)``.

    ``"runner:model"`` (e.g. ``"claude:claude-sonnet-4-6"``) pins a runner for that
    brain; a bare model uses the role's default runner. Only a recognised
    runner-kind prefix is treated as a runner, so ``"opencode-go/kimi"`` and
    other slash-style ids stay plain models.

    Checked against the *live* registry (``RUNNER_MAP``) rather than the
    static ``KNOWN_RUNNER_KINDS`` tuple — so plugin-contributed runner kinds
    are recognised as prefixes, and advertised-but-unregistered orphan kinds
    (e.g. the historical ``"api"`` kind; see roadmap Phase 26a) are not,
    consistent with how the registry resolves kinds at execution time.
    """
    from hivepilot.registry import RUNNER_MAP

    if ":" in entry:
        prefix, rest = entry.split(":", 1)
        if prefix in RUNNER_MAP:
            return prefix, rest
    return default_runner, entry


# ---------------------------------------------------------------------------
# Debate synthesis judge (Debate Judge & Consensus PRD, Sprint 1)
#
# `Verdict` and `Orchestrator._adjudicate` are a STABLE shared contract reused
# as-is by Sprint 2 — do not change the shape or parsing rules without
# documenting the change (see the sprint's Agent Notes for the full contract).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """A judge's synthesis of a debate's positions.

    ``decision`` is ``None`` (never a fabricated string) when the judge's raw
    output was empty, malformed, non-JSON, or missing a confident decision —
    callers MUST treat ``decision is None`` as "no confident decision" and
    fall back to the templated/majority-stance path.
    """

    decision: str | None
    confidence: float | None
    per_role_stance: dict[str, str] | None = None


# Judge synthesis prompt (Sprint 1 contract — Sprint 2 reuses this verbatim
# unless documented otherwise). Instructs the judge to return ONLY a JSON
# object matching the `_parse_verdict` parse rules below.
_JUDGE_PROMPT_TEMPLATE = (
    "You are an impartial arbiter reviewing a multi-model debate.\n\n"
    "TOPIC:\n{topic}\n\n"
    "POSITIONS SUBMITTED:\n{positions_block}\n\n"
    "Read every rationale carefully, weigh the arguments on their merits, and "
    "synthesize ONE final decision.\n\n"
    "Respond with ONLY a single JSON object -- no prose, no markdown code "
    "fences -- matching exactly this shape:\n"
    '{{"decision": "<final decision, one paragraph>", '
    '"confidence": <float between 0.0 and 1.0>, '
    '"per_role_stance": {{"<role>": "<one-line stance>"}}}}\n\n'
    "If you cannot reach a confident decision, respond with "
    '{{"decision": null, "confidence": 0.0}} -- never fabricate a decision you '
    "are not confident about."
)


def _build_judge_prompt(topic: str, positions: list[Position]) -> str:
    """Render the judge synthesis prompt for *positions* on *topic*."""
    positions_block = "\n".join(f"- {p.role}: {p.rationale}" for p in positions)
    return _JUDGE_PROMPT_TEMPLATE.format(topic=topic, positions_block=positions_block)


def _parse_verdict(raw: str) -> Verdict:
    """Parse the judge's raw text response into a :class:`Verdict`.

    Parse rules (Sprint 1 contract):
      * Empty/whitespace-only text -> no confident decision.
      * Tolerates a ```json ... ``` fenced block around the JSON object.
      * Non-JSON or a non-object JSON value -> no confident decision.
      * ``decision`` must be a non-empty string after stripping; ``null``,
        missing, or empty -> no confident decision.
      * ``confidence`` must be an ``int``/``float`` (bool excluded); missing
        or non-numeric -> no confident decision even if ``decision`` parsed.
        A numeric value is clamped into ``[0.0, 1.0]``.
      * ``per_role_stance``, when present, must be a ``dict[str, str]`` or it
        is dropped (does not invalidate the rest of the verdict).

    NEVER fabricates a decision: any of the above failures returns
    ``Verdict(decision=None, confidence=None, per_role_stance=None)``.
    """
    text = raw.strip() if raw else ""
    if not text:
        return Verdict(decision=None, confidence=None)

    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return Verdict(decision=None, confidence=None)

    if not isinstance(data, dict):
        return Verdict(decision=None, confidence=None)

    decision = data.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        return Verdict(decision=None, confidence=None)

    confidence_raw = data.get("confidence")
    if (
        not isinstance(confidence_raw, (int, float))
        or isinstance(confidence_raw, bool)
        or not math.isfinite(confidence_raw)
    ):
        # Non-numeric, bool, or non-finite (NaN/Infinity, which json.loads
        # accepts by default) confidence is untrustworthy -> no confident
        # decision. A garbage response must never become MAX confidence.
        return Verdict(decision=None, confidence=None)
    confidence = max(0.0, min(1.0, float(confidence_raw)))

    per_role_stance: dict[str, str] | None = None
    stance_raw = data.get("per_role_stance")
    if isinstance(stance_raw, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in stance_raw.items()
    ):
        per_role_stance = dict(stance_raw)

    return Verdict(
        decision=decision.strip(), confidence=confidence, per_role_stance=per_role_stance
    )


# ---------------------------------------------------------------------------
# Independent challenge arbiter (Debate Judge & Consensus PRD, Sprint 2)
#
# Opt-in (`settings.enable_challenge_arbiter`) THIRD-party judge for the
# challenge/rebuttal resolution check in `Orchestrator._run_rebuttal_round`,
# reusing the Sprint 1 `Verdict` / `_parse_verdict` contract as-is. The judge
# is never the challenger and never the target — it adjudicates independently
# so neither party self-grades the outcome.
# ---------------------------------------------------------------------------

_CHALLENGE_ARBITER_PROMPT_TEMPLATE = (
    "You are an impartial arbiter adjudicating a challenge between two colleagues. "
    "You are independent of both — you are neither the challenger nor the target.\n\n"
    "TARGET ({target_name})'S PRIOR OUTPUT:\n{prior_output}\n\n"
    "CHALLENGE from {challenger_name}:\n{challenge_point}\n\n"
    "TARGET'S REBUTTAL:\n{rebuttal_output}\n\n"
    "Weigh the challenge against the rebuttal on their merits and decide whether "
    "the rebuttal adequately resolves the challenge.\n\n"
    "Respond with ONLY a single JSON object -- no prose, no markdown code "
    "fences -- matching exactly this shape:\n"
    '{{"decision": "ACCEPT" or "DEFEND", "confidence": <float between 0.0 and 1.0>}}\n\n'
    '"ACCEPT" means the rebuttal adequately resolves the challenge. "DEFEND" means '
    "the challenge stands and should be escalated for human review. If you cannot "
    'reach a confident decision, respond with {{"decision": null, "confidence": 0.0}} '
    "-- never fabricate a decision you are not confident about."
)


def _build_challenge_arbiter_prompt(
    *,
    target_name: str,
    challenger_name: str,
    challenge_point: str,
    prior_output: str,
    rebuttal_output: str,
) -> str:
    """Render the challenge-arbiter resolution prompt (Sprint 2 contract)."""
    return _CHALLENGE_ARBITER_PROMPT_TEMPLATE.format(
        target_name=target_name,
        challenger_name=challenger_name,
        challenge_point=challenge_point[:2000],
        prior_output=(prior_output[:2000] if prior_output else "(not available)"),
        rebuttal_output=rebuttal_output[:2000],
    )


def _parse_components(text: str, valid: list[str]) -> list[str]:
    """Extract the component subset an agent selected via a ``COMPONENTS:`` line,
    intersected with the *valid* component set. Returns ``[]`` if none matched
    (callers fall back to all components)."""
    import re

    valid_set = set(valid)
    found: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*COMPONENTS\s*:\s*(.+)", line, re.IGNORECASE)
        if not m:
            continue
        for tok in re.split(r"[,\s]+", m.group(1).strip()):
            tok = tok.strip().strip(".`*")
            if tok in valid_set and tok not in found:
                found.append(tok)
    return found


def _parse_output_sections(text: str, keys: list[str]) -> dict[str, str]:
    """Extract per-key ``## <HEADER>`` sections from a stage's output text.

    A *key* (e.g. ``"design_spec"``) matches a header (e.g. ``"## DESIGN_SPEC"``,
    ``"## Design Spec"``, or ``"## design-spec"``) when, after upper-casing both
    and collapsing runs of ``_``, ``-``, and whitespace to a single ``_``, they
    are equal. A section's body is every line following its header up to the
    next ``## `` header (matched or not) or the end of *text*, with
    leading/trailing blank lines stripped.

    Returns ``{key: section_body}`` only for keys whose section was found —
    mirrors ``_parse_components``'s "empty when none found" style so callers
    can fall back (here: the whole-blob coarse fallback in the stage loop,
    see ``_stage_outputs_by_key``)."""
    import re

    def _normalize(header: str) -> str:
        return re.sub(r"[\s_-]+", "_", header.strip()).upper()

    key_by_norm = {_normalize(k): k for k in keys}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^##(?!#)\s+(.+?)\s*$", line)
        if m:
            current = key_by_norm.get(_normalize(m.group(1)))
            if current is not None:
                sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _stage_outputs_by_key(stage_output: str, keys: list[str]) -> dict[str, str]:
    """Map a producing stage's declared output *keys* to content for the
    run-scoped keyed store (PRD A2): section-extracted where a ``## <KEY>``
    header is present in *stage_output*, else the whole *stage_output* blob
    (coarse fallback) so every declared key always resolves to something.

    Built but not consumed this sprint — see PRD A2 Sprint 1. Callers merging
    this into a run-scoped dict across stages should let later stages
    overwrite same-key entries from earlier stages (last producer wins)."""
    sections = _parse_output_sections(stage_output, keys)
    return {key: sections.get(key, stage_output) for key in keys}


def _resolve_stage_target_components(
    stage: PipelineStage, group_tags: dict[str, list[str]]
) -> set[str]:
    """Union of a stage's ``only_components`` and the components tagged by any
    of its ``only_tags`` (resolved against *group_tags*, tag -> component names).

    Returns an empty set when the stage declares neither selector.
    """
    target = set(stage.only_components or [])
    for tag in stage.only_tags or []:
        target |= set(group_tags.get(tag, []))
    return target


def _validate_stage_tags(stages: list[PipelineStage], group_tags: dict[str, list[str]]) -> None:
    """Fail-closed guard: every ``only_tags`` value referenced by any stage must
    be defined in the run's ``Group.tags``.

    Raises ``ValueError`` naming the offending tag on the first mismatch found.
    Called once, up front (before any stage executes), so a mistyped or
    undefined tag on e.g. a review/security stage can never be silently
    bypassed by that stage instead running unscoped or being skipped.
    """
    for stage in stages:
        for tag in stage.only_tags or []:
            if tag not in group_tags:
                raise ValueError(
                    f"Pipeline stage '{stage.name}' references undefined tag "
                    f"'{tag}' (not present in this run's Group.tags)"
                )


def _stage_should_skip(
    stage: PipelineStage,
    group_tags: dict[str, list[str]],
    selected_components: list[str],
) -> bool:
    """A stage is skipped iff its scoping target (``only_components`` union
    ``only_tags``-resolved components) is non-empty AND disjoint from the
    components selected for this run. A stage with neither selector set
    always runs (target is empty -> never skipped)."""
    target = _resolve_stage_target_components(stage, group_tags)
    if not target:
        return False
    return target.isdisjoint(selected_components)


def build_prior_context(
    prior_chunks: list[str],
    mode: str,
    max_chars: int,
) -> str | None:
    """Build the prior_context string to pass to the next stage.

    Modes:
    - full: join all chunks unchanged.
    - synthesis: keep only the chunk whose header contains "Plan Synthesis"
      (case-insensitive) if found, plus the most recent chunk. If no synthesis
      chunk exists, keep only the most recent chunk.
    - cap: join all, truncate to max_chars keeping the TAIL (most recent content),
      prepend '…[earlier context truncated]…' if truncation occurred.

    Returns None if prior_chunks is empty.
    """
    if not prior_chunks:
        return None
    if mode == "synthesis":
        synthesis = next((c for c in prior_chunks if "plan synthesis" in c.lower()), None)
        last = prior_chunks[-1]
        parts = []
        if synthesis and synthesis is not last:
            parts.append(synthesis)
        parts.append(last)
        return "\n\n".join(parts)
    joined = "\n\n".join(prior_chunks)
    if mode == "cap":
        if len(joined) > max_chars:
            truncated = joined[-max_chars:]
            return "\u2026[earlier context truncated]\u2026\n\n" + truncated
        return joined
    # mode == "full"
    return joined


def _route_prior_context(
    *,
    role: "Role | None",
    prior_chunks: list[str],
    outputs_by_key: dict[str, str],
    routing_mode: str,
    prior_context_mode: str,
    max_chars: int,
    stage_name: str,
) -> str | None:
    """Compute the prior_context string for the stage about to run (PRD A2 Sprint 2).

    Routing is gated ONLY on ``routing_mode == "keyed"`` — NEVER on whether
    *role* declares ``inputs``. ``roles.yaml`` already declares ``inputs`` on
    EVERY role (cosmetically), so gating on presence-of-inputs instead of the
    explicit flag would silently regress every existing pipeline to a keyed
    subset. In ``full`` mode (default) this always falls through to
    ``build_prior_context(prior_chunks, ...)`` — byte-identical to
    pre-Sprint-2 behaviour for every role, regardless of its declared inputs.

    In ``keyed`` mode, a role with non-empty ``inputs`` and/or
    ``optional_inputs`` gets its context assembled from ONLY the declared
    input keys present in *outputs_by_key* (Sprint 1's run-scoped store),
    joined as ``## <KEY>`` blocks and capped with the same tail-truncation
    rule as ``build_prior_context``'s "cap" mode. ``optional_inputs`` keys are
    routed in when present but never count toward the "missing" fallback
    check below -- they document keys a role may consume when an upstream
    stage happens to produce them (e.g. a role shared across pipelines,
    only some of which run the producing stage), so their absence is
    expected and must not trigger the conservative full-context fallback.

    Conservative fallback rule:
    - ALL declared REQUIRED input keys missing from the store -> the keyed
      slice would be empty (or optional-only), which is worse than no
      routing at all, so fall back to the full
      ``build_prior_context(prior_chunks, ...)`` and log a warning naming
      the missing required keys.
    - SOME (not all) declared required input keys present, or any optional
      input keys present -> use exactly what's present; a non-empty keyed
      subset is still more precise than the full context, so no fallback in
      that case.
    - Role has no declared ``inputs`` and no ``optional_inputs`` (both empty)
      -> not routable at all; falls through to the full context, same as
      ``full`` mode.
    """
    if routing_mode == "keyed" and role is not None and (role.inputs or role.optional_inputs):
        keys = list(role.inputs) + [k for k in role.optional_inputs if k not in role.inputs]
        present = {k: outputs_by_key[k] for k in keys if k in outputs_by_key}
        if present:
            joined = "\n\n".join(f"## {k.upper()}\n{v}" for k, v in present.items())
            if len(joined) > max_chars:
                return "\u2026[earlier context truncated]\u2026\n\n" + joined[-max_chars:]
            return joined
        missing = [k for k in role.inputs if k not in outputs_by_key]
        logger.warning(
            "pipeline.keyed_context_fallback",
            stage=stage_name,
            missing_keys=missing,
        )
    return build_prior_context(prior_chunks, mode=prior_context_mode, max_chars=max_chars)


@dataclass
class RunResult:
    project: str
    target: str
    success: bool
    detail: str | None = None
    # True for a stage skipped via only_components/only_tags scoping — distinct
    # from both success and failure in the run record (PRD A1 §6).
    skipped: bool = False


class Orchestrator:
    def __init__(self, plugins: PluginManager | None = None) -> None:
        # Phase 26b — optional shared PluginManager injection. Default
        # (`None`) preserves EXACT prior behavior for every existing caller
        # (CLI, API server, tests, `drift_schedule.py`): `_load()` builds a
        # fresh, independent `PluginManager()` per `Orchestrator()`, same as
        # before this param existed.
        #
        # When a caller passes one explicitly (the scheduler daemon's
        # `plugins_hot_reload` path — see `scheduler_daemon.py`), `_load()`
        # REUSES it instead of constructing a second one. This matters
        # because `RUNNER_MAP`/`NOTIFIER_MAP`/`SECRETS_MAP`
        # (`hivepilot.registry`/`hivepilot.services.notification_service`)
        # are process-global: a SECOND, independent `PluginManager()` that
        # re-scans the SAME `plugins/*.py` a first one already registered
        # runner/notifier/secrets kinds for would see those kinds already
        # live but (having its own, empty ownership) NOT owned by it, and
        # raise a collision — breaking dispatch. Injecting one shared
        # manager into every `Orchestrator()` a caller constructs means
        # exactly one `PluginManager` ever registers into those globals, and
        # a later `reload()` on that shared manager genuinely changes what
        # subsequent dispatches see (not just observability).
        self._injected_plugins = plugins
        self._load()
        # Reentrancy guard for the resolved-secrets masking registry
        # (config_provenance._SECRET_VALUES): run_pipeline calls run_task once
        # per stage, so a naive "clear on run_task exit" would empty the
        # registry BEFORE run_pipeline's own post-stage sinks (write_stage_
        # artifact, record_interaction, stream_agent_turn) run — defeating
        # their redaction. Tracking scope depth means the registry is only
        # cleared when the OUTERMOST run_task/run_pipeline call exits, after
        # every sink for that run (nested or not) has already redacted
        # against it. See _enter_run_scope / _exit_run_scope.
        self._run_scope_lock = threading.Lock()
        self._run_scope_depth = 0
        # Fail-closed PR-gate aggregate (Debate Judge & Consensus PRD, Sprint 3).
        # The most-blocking Verdict seen so far THIS run — see
        # `_register_verdict` and the `perform_git_actions` call site in
        # `_execute_task_body`. Reset per outermost run (see
        # `_enter_run_scope`) so a stale verdict from a prior unrelated
        # run_task/run_pipeline/run_debate call never leaks into this one.
        self._verdict_lock = threading.Lock()
        self._governing_verdict: Verdict | None = None

    def _load(self) -> None:
        self.projects = load_projects()
        self.tasks = load_tasks()
        self.pipelines = load_pipelines()
        self.registry = RunnerRegistry(self.tasks.runners)
        self.plugins = (
            self._injected_plugins if self._injected_plugins is not None else PluginManager()
        )

    def refresh(self) -> None:
        self._load()

    def _enter_run_scope(self) -> None:
        with self._run_scope_lock:
            self._run_scope_depth += 1
            entering_outermost = self._run_scope_depth == 1
        if entering_outermost:
            with self._verdict_lock:
                self._governing_verdict = None

    def _effective_debate(
        self,
        stage: "PipelineStage | None",
        pipeline: "PipelineConfig | None",
    ) -> EffectiveDebateConfig:
        """Resolve the effective debate/consensus config for *stage* within
        *pipeline* (debate-judge-pipeline-yaml PRD, Sprint 2) — the SINGLE
        choke point every debate-judge / challenge-arbiter / fail-closed-gate
        call site in this module goes through, instead of reading
        `settings.enable_debate_judge` / `enable_challenge_arbiter` /
        `judge_runner` / `judge_model` / `judge_confidence_threshold`
        directly. See `resolve_debate_config` for the strengthen-only
        precedence over the global settings floor.

        `stage`/`pipeline` are `None` for a plain (non-pipeline) task run or
        a standalone `run_debate` call — resolves to the floor only, byte-
        identical to pre-Sprint-2 behaviour.
        """
        return resolve_debate_config(pipeline=pipeline, stage=stage)

    def _register_verdict(
        self, verdict: "Verdict | None", *, confidence_threshold: float | None = None
    ) -> None:
        """Track the most-blocking :class:`Verdict` seen so far this run
        (Debate Judge & Consensus PRD, Sprint 3) — fail-closed aggregate for
        the `perform_git_actions` PR gate: "if more than one verdict applies,
        the most-blocking wins."

        Once ANY registered verdict is blocking (per `git_service.is_blocking`
        at *confidence_threshold* — the caller's own resolved
        `EffectiveDebateConfig.confidence_threshold`, see `_effective_debate`;
        defaults to the floor-only threshold when the caller has no
        pipeline/stage context of its own), it becomes STICKY — a later
        approving verdict from an unrelated stage/challenge never erases an
        earlier one that failed the gate. A no-op on `None` (nothing to
        register) — the gate's own `is_blocking(None, ...)` call already
        fails closed on "no verdict reached the gate" without this method
        needing to do anything.
        """
        if verdict is None:
            return
        from hivepilot.services.git_service import is_blocking

        threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else self._effective_debate(None, None).confidence_threshold
        )
        with self._verdict_lock:
            if self._governing_verdict is not None and is_blocking(
                self._governing_verdict, threshold
            ):
                return  # already blocking — stays blocking (sticky)
            self._governing_verdict = verdict

    def _exit_run_scope(self) -> None:
        """Exit a run_task/run_pipeline scope; clear the resolved-secrets
        registry only when the outermost scope exits (depth back to 0)."""
        with self._run_scope_lock:
            self._run_scope_depth = max(0, self._run_scope_depth - 1)
            should_clear = self._run_scope_depth == 0
        if should_clear:
            from hivepilot.services.config_provenance import clear_secret_values

            clear_secret_values()

    def run_task(
        self,
        *,
        project_names: Iterable[str],
        task_name: str,
        extra_prompt: str | None,
        auto_git: bool,
        concurrency: int | None = None,
        simulate: bool = False,
        dry_run: bool = True,
        prior_context: str | None = None,
        mode: str = "cli",
        stage_skills: list[str] | None = None,
        stage_model: str | None = None,
        stage_effort: EffortLevel | None = None,
        stage: "PipelineStage | None" = None,
        pipeline: "PipelineConfig | None" = None,
    ) -> list[RunResult]:
        """Public entry point. Delegates to `_run_task_body` inside a run-scope
        (see `_enter_run_scope`/`_exit_run_scope`) so the resolved-secrets
        masking registry is cleared once this run — including any pipeline
        stages nested inside it when called from `run_pipeline` — is fully
        complete and every sink has already redacted against it.

        ``mode`` is the pipeline/stage-resolved execution mode (see
        ``resolve_mode``); it is injected into each step's runner dispatch and
        validated against the runner's ``supported_modes``. Defaults to
        ``"cli"`` for a plain task run, keeping existing behaviour unchanged.

        ``stage_model``/``stage_effort`` are the pipeline/stage-resolved
        model + reasoning-effort defaults (see ``resolve_stage_model``/
        ``resolve_effort``) — threaded through to each role-driven step's
        runner dispatch via ``hivepilot.roles.resolve_stage_dispatch``.
        Default ``None`` for a plain task run, keeping existing behaviour
        byte-identical (same "stage over role over runner-default" precedence
        ``stage_skills`` already follows).

        ``stage``/``pipeline`` are the raw enclosing `PipelineStage`/
        `PipelineConfig` objects (debate-judge-pipeline-yaml PRD, Sprint 2) —
        threaded through to `_execute_task_body`'s `_effective_debate` call
        for the dual-model-debate judge and the fail-closed verdict gate.
        Both default `None` for a plain (non-pipeline) task run, resolving to
        the settings floor only — byte-identical to pre-Sprint-2 behaviour."""
        self._enter_run_scope()
        try:
            return self._run_task_body(
                project_names=project_names,
                task_name=task_name,
                extra_prompt=extra_prompt,
                auto_git=auto_git,
                concurrency=concurrency,
                simulate=simulate,
                dry_run=dry_run,
                prior_context=prior_context,
                mode=mode,
                stage_skills=stage_skills,
                stage_model=stage_model,
                stage_effort=stage_effort,
                stage=stage,
                pipeline=pipeline,
            )
        finally:
            self._exit_run_scope()

    def _run_task_body(
        self,
        *,
        project_names: Iterable[str],
        task_name: str,
        extra_prompt: str | None,
        auto_git: bool,
        concurrency: int | None = None,
        simulate: bool = False,
        dry_run: bool = True,
        prior_context: str | None = None,
        mode: str = "cli",
        stage_skills: list[str] | None = None,
        stage_model: str | None = None,
        stage_effort: EffortLevel | None = None,
        stage: "PipelineStage | None" = None,
        pipeline: "PipelineConfig | None" = None,
    ) -> list[RunResult]:
        if task_name not in self.tasks.tasks:
            raise ValueError(f"Unknown task: {task_name}")
        task = self.tasks.tasks[task_name]
        projects = [self._project(name) for name in project_names]
        run_dir = create_run_directory()
        limit = concurrency or settings.concurrency_limit
        results: list[RunResult] = []
        run_policies: dict[str, policy_service.Policy] = {}
        run_ids: dict[str, int] = {}
        notion_page_ids: dict[str, str | None] = {}
        immediate_projects: list[ProjectConfig] = []
        for project in projects:
            policy = policy_service.enforce_policy(project.path.name, auto_git=auto_git)
            run_policies[project.path.name] = policy
            severity = policy.block_on_severity
            if policy.require_approval and not simulate:
                run_id = state_service.record_run_start(
                    project.path.name, task_name, status="pending"
                )
                approval_meta = {
                    "task": task_name,
                    "project": project.path.name,
                    "extra_prompt": extra_prompt,
                    "auto_git": auto_git,
                }
                state_service.record_approval_request(
                    run_id, project.path.name, task_name, approval_meta
                )
                notification_service.send_approval_keyboard(
                    run_id=run_id, project=project.path.name, task=task_name
                )
                results.append(
                    RunResult(
                        project.path.name, task_name, False, f"Pending approval (run {run_id})"
                    )
                )
            elif (
                severity
                and not simulate
                and (
                    cve_block_detail := self._cve_gate_block_detail(
                        project, policy.scan_tool, severity
                    )
                )
                is not None
            ):
                # Phase 21 Sprint 2 — pipeline CVE gate. Mirrors the
                # `require_approval` branch above: a run-level pre-execution
                # gate that records a failed run and never adds `project` to
                # `immediate_projects`, so no step is ever executed.
                run_id = state_service.record_run_start(
                    project.path.name, task_name, status="running"
                )
                state_service.complete_run(run_id, "failed", cve_block_detail)
                notification_service.send_notification(
                    f"⛔ {project.path.name}: {task_name} blocked by CVE gate"
                )
                results.append(RunResult(project.path.name, task_name, False, cve_block_detail))
            else:
                run_id = state_service.record_run_start(
                    project.path.name, task_name, status="running"
                )
                run_ids[project.path.name] = run_id
                immediate_projects.append(project)
                try:
                    from hivepilot.services.notion_service import on_run_start

                    notion_page_ids[project.path.name] = on_run_start(
                        run_id=run_id, project=project.path.name, task=task_name
                    )
                except Exception:  # noqa: BLE001
                    pass
                notification_service.send_notification(
                    f"Starting {task_name} on {project.path.name}"
                )

        # Batch limiting: if dev_batch_size > 0, only dispatch the first N projects
        # and defer the rest as quota-deferred entries (picked up by daemon).
        if settings.dev_batch_size > 0 and len(immediate_projects) > settings.dev_batch_size:
            from datetime import datetime as _dt
            from datetime import timedelta
            from datetime import timezone as _tz

            from hivepilot.services.retry_service import enqueue_deferred as _enqueue_deferred

            _batch = immediate_projects[: settings.dev_batch_size]
            _remainder = immediate_projects[settings.dev_batch_size :]
            immediate_projects = _batch
            _defer_at = _dt.now(_tz.utc) + timedelta(minutes=1)
            for _dp in _remainder:
                _enqueue_deferred(
                    task=task_name,
                    projects=[_dp.path.name],
                    error="batch limit: deferred to next window",
                    next_retry_at=_defer_at,
                    context={
                        "task": task_name,
                        "extra_prompt": extra_prompt,
                        "auto_git": auto_git,
                    },
                )
                results.append(
                    RunResult(
                        _dp.path.name,
                        task_name,
                        False,
                        f"batch-deferred until {_defer_at}",
                    )
                )
                logger.info(
                    "run.batch_deferred",
                    project=_dp.path.name,
                    task=task_name,
                    batch_size=settings.dev_batch_size,
                )

        # Capture the currently-active OTel context (e.g. the `pipeline.run`
        # span opened by `run_pipeline`) so the `task.run`/`step.run` spans
        # `_execute_task` opens INSIDE the ThreadPoolExecutor worker thread
        # below still nest under it — contextvars are NOT automatically
        # inherited by threads spawned by `ThreadPoolExecutor.submit`. No-op
        # when OTel isn't installed (`current_context()` returns `None`).
        _otel_ctx = current_context()

        def _execute_task_traced(**kwargs):
            with use_context(_otel_ctx):
                return self._execute_task(**kwargs)

        with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as executor:
            future_map = {
                executor.submit(
                    _execute_task_traced,
                    project=project,
                    task_name=task_name,
                    task=task,
                    extra_prompt=extra_prompt,
                    auto_git=auto_git,
                    run_id=run_ids.get(project.path.name),
                    policy=run_policies.get(project.path.name),
                    simulate=simulate,
                    dry_run=dry_run,
                    prior_context=prior_context,
                    mode=mode,
                    stage_skills=stage_skills,
                    stage_model=stage_model,
                    stage_effort=stage_effort,
                    stage=stage,
                    pipeline=pipeline,
                ): project
                for project in immediate_projects
            }
            from hivepilot.services.quota import QuotaDeferredError

            for future in concurrent.futures.as_completed(future_map):
                project = future_map[future]
                try:
                    detail = future.result()
                    # Phase 10c: `detail` here is `_execute_task`'s joined
                    # step-output string (whatever the runner's `capture()`
                    # returned) — this is the RunResult choke point every
                    # downstream sink (cli.py's `typer.echo(result.detail)`,
                    # api_service's `/v1/run` response body, and the
                    # discord/slack/telegram `_format_results` chat replies)
                    # ultimately reads from. Those sinks do NOT redact
                    # themselves (unlike state_service/notification_service/
                    # artifact writers — see config_provenance's sink list),
                    # so a runner that echoes a resolved `${secret:}` value in
                    # its stdout would otherwise leak it verbatim to a chat
                    # channel or API response. Masking here means every sink
                    # gets the already-redacted text for free.
                    #
                    # Known limitation: `redact_text` only masks values that
                    # were actually `register_secret_value`'d (i.e. resolved
                    # via `${secret:NAME}` — see `_resolve_secrets`). A secret
                    # echoed from a source HivePilot never registered — e.g.
                    # terraform state, a data-source lookup, or a hardcoded
                    # plaintext value in `definition.env` — will NOT be
                    # caught. This is necessary-but-not-sufficient; the full
                    # fix would also register `definition.env` secret-shaped
                    # values at resolution time (out of scope here).
                    detail = redact_text(detail) if detail else detail
                    results.append(RunResult(project.path.name, task_name, True, detail))
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "success")
                    try:
                        from hivepilot.services.notion_service import on_run_complete

                        on_run_complete(notion_page_ids.get(project.path.name), status="success")
                    except Exception:  # noqa: BLE001
                        pass
                    notification_service.send_notification(
                        f"✅ {project.path.name}: {task_name} completed"
                    )
                except QuotaDeferredError as exc:  # noqa: BLE001
                    from datetime import datetime as _datetime
                    from datetime import timedelta, timezone

                    from hivepilot.services.retry_service import enqueue_deferred

                    _reset_at = exc.reset_at or (
                        _datetime.now(timezone.utc) + timedelta(minutes=30)
                    )
                    enqueue_deferred(
                        task=task_name,
                        projects=[project.path.name],
                        error=str(exc),
                        next_retry_at=_reset_at,
                        context={
                            "task": task_name,
                            "extra_prompt": extra_prompt,
                            "auto_git": auto_git,
                        },
                    )
                    logger.warning(
                        "run.quota_deferred",
                        project=project.path.name,
                        task=task_name,
                        reset_at=str(_reset_at),
                    )
                    results.append(
                        RunResult(
                            project.path.name,
                            task_name,
                            False,
                            f"quota-deferred until {_reset_at}",
                        )
                    )
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "deferred")
                except StepApprovalPending as exc:
                    # Not a failure: `_execute_task` already recorded an
                    # approval request and marked the run PAUSED before
                    # raising. Do NOT call complete_run here — that would
                    # overwrite the PAUSED status this exception carries.
                    logger.info(
                        "run.step_approval_pending",
                        project=project.path.name,
                        task=task_name,
                        error=str(exc),
                    )
                    results.append(
                        RunResult(
                            project.path.name,
                            task_name,
                            False,
                            f"Pending approval (run {run_ids.get(project.path.name)}): {exc}",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "run.failure", project=project.path.name, task=task_name, error=str(exc)
                    )
                    # Phase 10c: a raised runner/step error can itself echo
                    # captured output (e.g. "non-zero exit: <stdout>") — mask
                    # registered secrets here too, same choke-point rationale
                    # as the success-path `detail` above. Compute this ONCE
                    # and reuse it for every downstream sink in this block
                    # (RunResult, Notion, Linear) — they all receive the same
                    # exception message, so a single redaction point avoids a
                    # raw `str(exc)` slipping through to any one of them.
                    exc_text = redact_text(str(exc))
                    results.append(RunResult(project.path.name, task_name, False, exc_text))
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "failed", str(exc))
                    notification_service.send_notification(
                        f"❌ {project.path.name}: {task_name} failed ({exc})"
                    )
                    try:
                        from hivepilot.services.notion_service import on_run_complete

                        on_run_complete(
                            notion_page_ids.get(project.path.name),
                            status="failed",
                            detail=exc_text,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        from hivepilot.services.linear_service import on_run_failure

                        on_run_failure(project=project.path.name, task=task_name, error=exc_text)
                    except Exception:  # noqa: BLE001
                        pass
        summary = {
            "task": task_name,
            "projects": [p.path.name for p in projects],
            "results": [result.__dict__ for result in results],
            "extra_prompt": extra_prompt,
        }
        write_summary(run_dir, summary)
        artifact_manager = ArtifactManager(run_dir)
        artifact_manager.write_json("results.json", summary)
        self._collect_artifacts(
            manager=artifact_manager,
            task=task,
            projects=projects,
        )
        exporters = task.artifacts.get("exporters", [])
        artifact_manager.export(exporters)
        project_lookup = {project.path.name: project for project in projects}
        # Per-pipeline-lessons-yaml PRD, Sprint 2: resolve ONCE per run,
        # reuse for every project result below -- `pipeline` is constant
        # for the whole `_run_task_body` call (mirrors `_effective_debate`'s
        # resolve-once-per-call discipline). `None` for a plain (non-
        # pipeline) task run resolves to the settings floor only, byte-
        # identical to reading `settings.enable_lesson_distillation`
        # directly (see `resolve_lessons_config`'s docstring).
        _effective_lessons = resolve_lessons_config(pipeline=pipeline)
        for result in results:
            project_cfg = project_lookup.get(result.project)
            if not project_cfg:
                continue
            summary_text = f"{result.target} -> {'success' if result.success else 'failed'} ({result.detail or 'no detail'})"
            knowledge_service.append_feedback(project_cfg.path, result.target, summary_text)
            # Auto-Learning Lessons Loop PRD, Sprint 2: opt-in, best-effort
            # per-project lesson distillation, right next to the
            # `append_feedback` call above -- same "one summary per
            # completed project result" granularity, correlated by the
            # SAME `run_id` the rest of this project's verdicts/
            # interactions/steps were persisted under. Both `simulate=True`
            # AND `dry_run=True` skip the real distiller call and every
            # persistence side effect: `simulate` mirrors
            # `Orchestrator._adjudicate`'s simulate branch (no real
            # `capture_definition` call -- a simulated run has no real
            # verdicts/interactions worth distilling anyway); `dry_run`
            # additionally gates it because distillation makes a REAL LLM
            # call (unlike `record_verdict`/`record_interaction`, which only
            # persist data already produced by a step that already ran) --
            # a dry run must never trigger a brand-new, costed side effect.
            # A failure here is caught and logged, never allowed to break
            # the pipeline -- same best-effort discipline as the
            # Notion/Linear notification calls a few lines above.
            #
            # Per-pipeline-lessons-yaml PRD, Sprint 2: the gate now reads
            # the RESOLVED per-pipeline config (`_effective_lessons.
            # enable_distillation`) instead of `settings.
            # enable_lesson_distillation` directly -- strengthen-only OR
            # across the floor + this pipeline's `lessons:` block (see
            # `resolve_lessons_config`), never weaker than the floor.
            if _effective_lessons.enable_distillation and not simulate and not dry_run:
                run_id = run_ids.get(result.project)
                if run_id is not None:
                    try:
                        self._distill_and_persist_lessons(
                            run_id=run_id,
                            project=project_cfg,
                            role=task.role,
                            task_name=task_name,
                            result=result,
                            lessons_config=_effective_lessons,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "lessons.distill_error",
                            project=project_cfg.path.name,
                            task=task_name,
                            error=redact_text(str(exc)),
                        )
        return results

    def _distill_and_persist_lessons(
        self,
        *,
        run_id: int,
        project: ProjectConfig,
        role: str | None,
        task_name: str,
        result: RunResult,
        lessons_config: "EffectiveLessonsConfig | None" = None,
    ) -> None:
        """Distill *this project's* verdicts + interactions + outcome from
        *run_id* into lesson CANDIDATEs via ONE `lessons_service.
        distill_lessons` call, then persist each via `state_service.
        record_lesson(..., validated=False)` (Auto-Learning Lessons Loop
        PRD, Sprint 2 -- Sprint 3 owns real validation).

        ``lessons_config`` (per-pipeline-lessons-yaml PRD, Sprint 2) is the
        caller's already-resolved `EffectiveLessonsConfig` -- `_run_task_
        body` always passes its ONE per-run resolution. `None` (a direct/
        standalone caller, e.g. a unit test invoking this method without
        going through `_run_task_body`) resolves to the floor only
        (`resolve_lessons_config(pipeline=None)`), byte-identical to
        reading `settings.lesson_distill_runner`/`.lesson_distill_model`/
        `.lesson_min_score` directly.

        Never raises past the caller's own try/except -- but this method
        itself does no swallowing so callers see the real error for
        logging."""
        from hivepilot.services import lessons_service

        _resolved = (
            lessons_config if lessons_config is not None else resolve_lessons_config(pipeline=None)
        )
        verdicts = state_service.list_recent_verdicts(run_id=run_id)
        interactions = state_service.list_recent_interactions(run_id=run_id)
        outcomes = [
            {
                "project": result.project,
                "target": result.target,
                "success": result.success,
                "detail": result.detail,
            }
        ]
        distiller_def = lessons_service.build_distiller_definition(
            runner=_resolved.distill_runner,
            model=_resolved.distill_model,
        )
        lessons = lessons_service.distill_lessons(
            run_id=run_id,
            project=project,
            role=role,
            task=task_name,
            verdicts=verdicts,
            interactions=interactions,
            outcomes=outcomes,
            distiller_def=distiller_def,
            capture_fn=self.registry.capture_definition,
        )
        if not lessons:
            return
        # Sprint 3: validate each freshly-persisted candidate against the
        # SAME run's REAL outcome signal -- never the distiller's own
        # self-report (see `lessons_service.validate_lesson`'s fail-closed
        # contract). Built ONCE per call (same run_id/result for every
        # candidate lesson from this project's run) from the *verdicts*/
        # *interactions* already fetched above -- no extra DB round-trip.
        outcome_signal = self._build_lesson_outcome_signal(
            result=result, verdicts=verdicts, interactions=interactions
        )
        for lesson in lessons:
            lesson_id = state_service.record_lesson(
                run_id=run_id,
                project=project.path.name,
                role=role,
                task=task_name,
                source_verdict_id=lesson.source_verdict_id,
                source_interaction_id=lesson.source_interaction_id,
                text=lesson.text,
                score=None,
                confidence=None,
                category=lesson.category,
                validated=False,
            )
            validated, score = lessons_service.validate_lesson(
                lesson, outcome_signal, min_score=_resolved.min_score
            )
            state_service.update_lesson_validation(lesson_id, validated=validated, score=score)

    def _build_lesson_outcome_signal(
        self,
        *,
        result: RunResult,
        verdicts: list[dict[str, Any]],
        interactions: list[dict[str, Any]],
    ) -> lessons_service.OutcomeSignal:
        """Build the REAL outcome signal `lessons_service.validate_lesson`
        gates on (Sprint 3) -- never the distiller's own self-report.

        ``run_success`` is the direct, real `RunResult.success` for the
        SAME project run this distillation call covers.

        ``resolved_challenge``/``max_verdict_confidence`` are deliberately
        scoped to ``kind == "challenge"`` verdicts/interactions ONLY -- not
        e.g. a plain ``"debate"`` verdict, which reflects the debate
        judge's confidence in ITS OWN routing decision, not whether this
        run's actual output held up under independent scrutiny.

        FAIL-CLOSED (post-review hardening -- CRITICAL fix): a challenge
        verdict only ever counts as a GENUINE, positive resolution when
        ALL of the following hold:
          * ``decision`` is ``"ACCEPT"`` (a ``"MAINTAIN"``/``"DEFEND"``
            verdict means the arbiter REJECTED the agent's work and the
            run was blocked/escalated -- see
            `tests/test_verdict_gate_failclosed.py` -- and must contribute
            NOTHING, not even its `confidence`, which on a rejected
            verdict means "how sure the work is BAD", not evidence a
            lesson from it is trustworthy);
          * ``confidence`` is a finite value in ``[0, 1]``;
          * ``confidence >= settings.lesson_min_score`` -- an ``"ACCEPT"``
            BELOW the floor is what `_resolve_challenge_via_arbiter`
            persists even when the challenge was actually escalated to a
            human (``accepted`` there requires the SAME floor check), so
            without this an escalated run could still count as "resolved"
            here.
        A verdict failing any of those contributes neither to
        `resolved_challenge` nor to `max_verdict_confidence` -- there is no
        partial credit. The SELF-adjudication path (`_run_rebuttal_round`)
        remains a separate, independent positive signal: it persists only
        an `interactions` row (`action="challenge"`,
        `summary="[RESOLUTION] ..."`) with no verdict row at all -- a
        resolution that does NOT start with "MAINTAIN" is resolved.
        """
        from hivepilot.services import lessons_service

        challenge_verdicts = [v for v in verdicts if v.get("kind") == "challenge"]
        genuine_accept_confidences: list[float] = []
        for v in challenge_verdicts:
            decision = v.get("decision")
            if not (isinstance(decision, str) and decision.strip().upper() == "ACCEPT"):
                continue
            confidence = v.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                continue
            if not (math.isfinite(confidence) and 0.0 <= confidence <= 1.0):
                continue
            if confidence < settings.lesson_min_score:
                continue
            genuine_accept_confidences.append(confidence)

        resolved_via_interaction = any(
            i.get("action") == "challenge"
            and isinstance(i.get("summary"), str)
            and "[RESOLUTION]" in i["summary"]
            and "MAINTAIN" not in i["summary"].upper()
            for i in interactions
        )
        return lessons_service.OutcomeSignal(
            run_success=bool(result.success),
            resolved_challenge=bool(genuine_accept_confidences) or resolved_via_interaction,
            max_verdict_confidence=(
                max(genuine_accept_confidences) if genuine_accept_confidences else None
            ),
        )

    def _agent_name(self, stage: PipelineStage) -> str:
        """Human-facing agent name for a stage (FR theme), falling back to stage name."""
        from hivepilot.roles import ROLES

        task = self.tasks.tasks.get(stage.task)
        if task and task.role:
            role = ROLES.get(task.role)
            if role and role.display_name:
                return f"{role.display_name} ({role.title})"
        return stage.name

    def _handle_agent_requests(
        self,
        *,
        stage_output: str,
        actor: str,
        stage: "PipelineStage",
        project_names: list[str],
        policy: "policy_service.Policy | None",
        budget: dict[str, int],
        depth: int = 0,
    ) -> str:
        """Process ``REQUEST: <agent> — <question>`` lines from *stage_output*.

        Guardrails (all enforced, highest-priority first):
        - ``depth >= settings.max_request_depth`` → skip (depth cap)
        - ``budget["remaining"] <= 0`` → skip (global budget exhausted)
        - per-turn cap: honour at most ``settings.max_agent_requests`` requests
        - anti-cycle guard: skip ``(requester, target)`` pairs seen in this turn
        - unresolvable target → skip with a warning log
        - on budget exhaustion or unresolved requests → append NEEDS_HUMAN note

        Each honoured request:
        1. Streams ❓ (requester → target — question)
        2. Re-invokes target role via ``capture_definition`` (same pattern as rebuttal)
        3. Streams ↩️ (target → requester — answer excerpt)
        4. Logs request + answer interactions
        5. Appends ``[ANSWER from <target>]: <answer>`` to the returned output

        Never raises — errors are caught and logged.
        """
        from typing import cast

        from hivepilot.models import RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_stage_dispatch
        from hivepilot.runners.base import RunnerPayload

        if depth >= settings.max_request_depth:
            return stage_output
        if budget["remaining"] <= 0:
            return stage_output + "\n[NEEDS_HUMAN] Agent request budget exhausted for this run."

        requests_found = parse_agent_requests(stage_output)
        if not requests_found:
            return stage_output

        # Per-turn cap
        requests_to_handle = requests_found[: settings.max_agent_requests]
        unhandled_count = len(requests_found) - len(requests_to_handle)

        # Anti-cycle guard: track (requester, target) pairs seen this turn
        seen_pairs: set[tuple[str, str]] = set()

        extra_lines: list[str] = []
        for target_display, question in requests_to_handle:
            if budget["remaining"] <= 0:
                extra_lines.append("[NEEDS_HUMAN] Agent request budget exhausted mid-turn.")
                break

            # Anti-cycle check
            pair = (actor, target_display)
            if pair in seen_pairs:
                logger.info(
                    "agent_request.cycle_skipped",
                    requester=actor,
                    target=target_display,
                )
                continue
            seen_pairs.add(pair)

            # Resolve target role key
            target_role_key = _resolve_role_from_display(target_display)
            if target_role_key is None:
                logger.info(
                    "agent_request.target_unresolvable",
                    requester=actor,
                    target=target_display,
                )
                extra_lines.append(
                    f"[NEEDS_HUMAN] Could not resolve agent '{target_display}' for request."
                )
                continue

            # Stream the request
            try:
                notification_service.stream_agent_request(
                    requester=actor,
                    target=target_display,
                    question=question,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_request.stream_failed", error=str(exc))

            # Log the request interaction
            try:
                log_request_interaction(actor=actor, target=target_display, question=question)
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_request.log_failed", error=str(exc))

            # Re-invoke target role via capture_definition
            answer = ""
            try:
                target_role = get_role(target_role_key)
                # No specific PipelineStage is resolvable for the TARGET role
                # here (`stage` above is the actor's own stage, not the
                # target's) — resolves identically to `resolve_runner`.
                runner_kind, role_model, req_effort = resolve_stage_dispatch(
                    target_role_key, policy
                )
                role_perm = target_role.permission_mode
                role_options: dict[str, str] = {}
                if role_perm:
                    role_options["permission_mode"] = role_perm
                req_runner_def = RunnerDefinition(
                    name=f"request:{target_role_key}",
                    kind=cast(RunnerKind, runner_kind),
                    command=None,
                    model=role_model,
                    effort=req_effort,
                    host=resolve_host(target_role_key, policy),
                    options=role_options,
                )
                req_prompt_file = (
                    str(target_role.prompt_file)
                    if target_role.prompt_file and target_role.prompt_file.exists()
                    else ""
                )
                req_step = TaskStep(
                    name=f"request:{target_role_key}",
                    runner=runner_kind,
                    prompt_file=req_prompt_file,
                )
                request_prompt = (
                    f"You are {target_display}. A colleague ({actor}) has a specific question.\n\n"
                    f"QUESTION: {question}\n\n"
                    "Give a concise, factual answer (under 200 words). "
                    "Focus only on the question asked."
                )
                req_payload = RunnerPayload(
                    project_name=project_names[0] if project_names else "unknown",
                    project=None,
                    task_name=f"request:{target_role_key}",
                    step=req_step,
                    metadata={"extra_prompt": request_prompt, "prior_context": ""},
                    secrets={},
                )
                answer = self.registry.capture_definition(req_runner_def, req_payload)
                budget["remaining"] -= 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent_request.invoke_failed",
                    target_role=target_role_key,
                    error=str(exc),
                )
                extra_lines.append(f"[NEEDS_HUMAN] Request to '{target_display}' failed: {exc}")
                continue

            # Stream the answer
            try:
                notification_service.stream_agent_answer(
                    target=target_display,
                    requester=actor,
                    answer_excerpt=answer[:500],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_answer.stream_failed", error=str(exc))

            # Log the answer interaction
            try:
                log_request_interaction(
                    actor=target_display, target=actor, question=f"[ANSWER] {answer[:500]}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_answer.log_failed", error=str(exc))

            extra_lines.append(f"[ANSWER from {target_display}]: {answer}")

        if unhandled_count > 0:
            extra_lines.append(
                f"[NEEDS_HUMAN] {unhandled_count} request(s) skipped due to per-turn cap "
                f"(max_agent_requests={settings.max_agent_requests})."
            )

        if extra_lines:
            return stage_output + "\n" + "\n".join(extra_lines)
        return stage_output

    def _run_rebuttal_round(
        self,
        *,
        challenger_name: str,
        challenge_target: str,
        challenge_point: str,
        challenger_stage: PipelineStage,
        completed_stages: list[PipelineStage],
        prior_chunks: list[str],
        policy: policy_service.Policy | None,
        project_name: str,
        simulate: bool,
        run_id: int | None = None,
        pipeline: "PipelineConfig | None" = None,
    ) -> None:
        """Execute one bounded rebuttal round after a ⚔️ challenge.

        *pipeline* is the enclosing `PipelineConfig` (debate-judge-pipeline-
        yaml PRD, Sprint 2), used alongside *challenger_stage* to resolve the
        effective debate/consensus config via `_effective_debate` — `None`
        (a call site outside a live pipeline run, e.g. a direct test call)
        resolves to the floor only, byte-identical to pre-Sprint-2 behaviour.

        Flow:
          1. Resolve target role from *challenge_target* display name.
          2. Confirm target is an upstream (already completed) stage.
          3. Build rebuttal prompt and invoke target via capture_definition.
          4. Stream 🛡️ rebuttal turn; log the interaction.
          5. Re-invoke challenger with rebuttal → "ACCEPT or MAINTAIN?".
          6. Stream ⚖️ (resolved) or 🙋 (escalated).
          7. Append resolution note to *prior_chunks* for downstream stages.

        Never raises — errors are logged and the pipeline continues with A-only
        ⚔️ visibility.
        """
        from typing import cast

        from hivepilot.models import RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_stage_dispatch
        from hivepilot.runners.base import RunnerPayload

        # Per-pipeline-lessons-yaml PRD, Sprint 2: resolve ONCE, reuse for
        # both `RunnerPayload`s this round constructs (rebuttal + challenger
        # resolution) -- mirrors `_execute_task_body`'s resolve-once
        # discipline. `pipeline=None` (a call site outside a live pipeline
        # run) resolves to the settings floor only.
        effective_lessons = resolve_lessons_config(pipeline=pipeline)

        # 1. Resolve target role key
        target_role_key = _resolve_role_from_display(challenge_target)
        if target_role_key is None:
            logger.info(
                "rebuttal.target_unresolvable",
                target=challenge_target,
                challenger=challenger_name,
            )
            return

        # 2. Confirm the target is an upstream completed stage
        target_role = get_role(target_role_key)
        # Find the completed stage whose task maps to this role
        target_stage: PipelineStage | None = None
        for s in completed_stages:
            task_cfg = self.tasks.tasks.get(s.task)
            if task_cfg and task_cfg.role == target_role_key:
                target_stage = s
                break
        if target_stage is None:
            logger.info(
                "rebuttal.target_not_upstream",
                target_role=target_role_key,
                target_display=challenge_target,
                completed=[s.task for s in completed_stages],
            )
            return

        # 3. Build rebuttal prompt for the target
        # Find the target's prior output from prior_chunks (search by agent display name)
        target_agent_name = self._agent_name(target_stage)
        prior_output = ""
        for chunk in reversed(prior_chunks):
            if target_agent_name in chunk or target_stage.name in chunk:
                prior_output = chunk
                break

        rebuttal_prompt = (
            f"You are {target_agent_name}. A colleague has challenged your previous output.\n\n"
            f"YOUR PREVIOUS OUTPUT:\n{prior_output[:2000] if prior_output else '(not available)'}\n\n"
            f"CHALLENGE from {challenger_name}:\n{challenge_point}\n\n"
            "Respond with one of:\n"
            "- ACCEPT: <brief acknowledgement and what you would change>\n"
            "- DEFEND: <clear rationale for why your original position stands>\n"
            "- ESCALATE: <brief statement of why this needs human review>\n\n"
            "Keep your response concise (under 200 words)."
        )

        # 4. Invoke target role's runner for rebuttal (the target's own
        # upstream stage's model/effort override this dispatch — see
        # `resolve_stage_dispatch`).
        runner_kind, role_model, rebuttal_effort = resolve_stage_dispatch(
            target_role_key,
            policy,
            stage_model=target_stage.model,
            stage_effort=target_stage.effort,
        )
        role_perm = target_role.permission_mode
        role_options: dict[str, str] = {}
        if role_perm:
            role_options["permission_mode"] = role_perm
        runner_def = RunnerDefinition(
            name=f"rebuttal:{target_role_key}",
            kind=cast(RunnerKind, runner_kind),
            command=None,
            model=role_model,
            effort=rebuttal_effort,
            host=resolve_host(target_role_key, policy),
            options=role_options,
        )
        prompt_file = (
            str(target_role.prompt_file)
            if target_role.prompt_file and target_role.prompt_file.exists()
            else ""
        )
        step = TaskStep(
            name=f"rebuttal:{target_role_key}",
            runner=runner_kind,
            prompt_file=prompt_file,
        )
        rebuttal_project = self._project(project_name)
        payload = RunnerPayload(
            project_name=project_name,
            project=rebuttal_project,
            task_name=f"rebuttal:{target_role_key}",
            step=step,
            metadata={"extra_prompt": rebuttal_prompt, "prior_context": ""},
            secrets={},
            lessons=effective_lessons,
        )

        if simulate:
            rebuttal_output = (
                f"[simulated rebuttal from {target_agent_name}] DEFEND: My analysis stands."
            )
        else:
            rebuttal_output = self.registry.capture_definition(runner_def, payload)

        # Stream 🛡️ rebuttal turn
        notification_service.stream_rebuttal(
            actor=target_agent_name,
            target=challenger_name,
            point=rebuttal_output,
        )
        log_challenge_interaction(
            actor=target_agent_name,
            target=challenger_name,
            point=f"[REBUTTAL] {rebuttal_output}",
        )

        # 5+6. Resolution check — either an INDEPENDENT third-party judge
        # arbiter (Sprint 2, opt-in via `enable_challenge_arbiter` — resolved
        # per-stage/pipeline via `_effective_debate`, see debate-judge-
        # pipeline-yaml PRD Sprint 2) or the pre-Sprint-2 default: the
        # challenger's OWN runner is re-invoked to self-adjudicate ACCEPT/
        # MAINTAIN. The arbiter path never lets either the challenger or the
        # target self-grade the outcome, and fails TOWARD human escalation on
        # any ambiguous/malformed/erroring verdict (see
        # `_resolve_challenge_via_arbiter` — never fails open).
        _debate_config = self._effective_debate(challenger_stage, pipeline)
        if _debate_config.enable_arbiter:
            is_escalated, resolution_output = self._resolve_challenge_via_arbiter(
                challenger_name=challenger_name,
                target_agent_name=target_agent_name,
                challenge_point=challenge_point,
                prior_output=prior_output,
                rebuttal_output=rebuttal_output,
                project=rebuttal_project,
                policy=policy,
                simulate=simulate,
                debate_config=_debate_config,
                run_id=run_id,
            )
        else:
            # 5. Re-invoke challenger with the rebuttal → resolution check
            challenger_role_key: str | None = None
            challenger_task_cfg2 = self.tasks.tasks.get(challenger_stage.task)
            if challenger_task_cfg2 and challenger_task_cfg2.role:
                challenger_role_key = challenger_task_cfg2.role

            resolution_prompt = (
                f"You are {challenger_name}. You challenged {target_agent_name} and they responded.\n\n"
                f"YOUR ORIGINAL CHALLENGE:\n{challenge_point}\n\n"
                f"THEIR REBUTTAL:\n{rebuttal_output[:2000]}\n\n"
                "Respond with exactly one of:\n"
                "- ACCEPT: <brief acknowledgement — you are satisfied with their defence>\n"
                "- MAINTAIN: <brief statement of why you still disagree — this will be escalated for human review>\n\n"
                "Keep your response concise (under 100 words)."
            )

            if challenger_role_key is None:
                # No role for challenger — default to ACCEPT to avoid blocking
                resolution_output = (
                    "ACCEPT: Unable to determine challenger role for resolution check."
                )
            elif simulate:
                resolution_output = (
                    f"[simulated resolution from {challenger_name}] ACCEPT: Satisfied."
                )
            else:
                # The challenger's OWN stage's model/effort override this
                # dispatch — mirrors the target dispatch above.
                ch_runner_kind, ch_role_model, ch_effort = resolve_stage_dispatch(
                    challenger_role_key,
                    policy,
                    stage_model=challenger_stage.model,
                    stage_effort=challenger_stage.effort,
                )
                ch_role = get_role(challenger_role_key)
                ch_role_perm = ch_role.permission_mode
                ch_role_options: dict[str, str] = {}
                if ch_role_perm:
                    ch_role_options["permission_mode"] = ch_role_perm
                ch_runner_def = RunnerDefinition(
                    name=f"resolution:{challenger_role_key}",
                    kind=cast(RunnerKind, ch_runner_kind),
                    command=None,
                    model=ch_role_model,
                    effort=ch_effort,
                    host=resolve_host(challenger_role_key, policy),
                    options=ch_role_options,
                )
                ch_step = TaskStep(
                    name=f"resolution:{challenger_role_key}",
                    runner=ch_runner_kind,
                    prompt_file=(
                        str(ch_role.prompt_file)
                        if ch_role.prompt_file and ch_role.prompt_file.exists()
                        else ""
                    ),
                )
                ch_payload = RunnerPayload(
                    project_name=project_name,
                    project=rebuttal_project,
                    task_name=f"resolution:{challenger_role_key}",
                    step=ch_step,
                    metadata={"extra_prompt": resolution_prompt, "prior_context": ""},
                    secrets={},
                    lessons=effective_lessons,
                )
                resolution_output = self.registry.capture_definition(ch_runner_def, ch_payload)

            # 6. Determine outcome
            is_escalated = resolution_output.strip().upper().startswith("MAINTAIN")

        # Stream final icon (shared by both resolution paths)
        if is_escalated:
            notification_service.stream_needs_human(
                actor=challenger_name,
                target=target_agent_name,
                point=resolution_output,
            )
            resolution_note = (
                f"[NEEDS_HUMAN] Challenge between {challenger_name} → {target_agent_name} "
                f"unresolved. Point: {challenge_point[:200]} | "
                f"Rebuttal: {rebuttal_output[:200]} | "
                f"Challenger maintained: {resolution_output[:200]}"
            )
        else:
            notification_service.stream_resolved(
                actor=challenger_name,
                target=target_agent_name,
                resolution=resolution_output,
            )
            resolution_note = (
                f"[RESOLVED] Challenge between {challenger_name} → {target_agent_name} closed. "
                f"Point: {challenge_point[:200]} | Resolution: {resolution_output[:200]}"
            )

        log_challenge_interaction(
            actor=challenger_name,
            target=target_agent_name,
            point=f"[RESOLUTION] {resolution_output}",
        )

        # 7. Append resolution note to prior_chunks for downstream context
        prior_chunks.append(f"## Challenge Resolution\n{resolution_note}")

    def _resolve_challenge_via_arbiter(
        self,
        *,
        challenger_name: str,
        target_agent_name: str,
        challenge_point: str,
        prior_output: str,
        rebuttal_output: str,
        project: ProjectConfig,
        policy: policy_service.Policy | None,
        simulate: bool,
        debate_config: EffectiveDebateConfig,
        run_id: int | None = None,
    ) -> tuple[bool, str]:
        """Resolve a challenge/rebuttal pair via the INDEPENDENT challenge
        arbiter (Debate Judge & Consensus PRD, Sprint 2).

        *debate_config* is the caller's (`_run_rebuttal_round`'s) already-
        resolved `EffectiveDebateConfig` — the runner/model/confidence
        threshold this arbiter call uses (see `_effective_debate`).

        Returns ``(is_escalated, resolution_output)`` — *resolution_output* is
        a human-readable summary of the verdict, in the same "text blob"
        shape the self-adjudication path produces, for `stream_needs_human` /
        `stream_resolved` / `log_challenge_interaction` to consume unchanged.

        FAILS TOWARD HUMAN ESCALATION — ``is_escalated`` is True (never a
        silent ACCEPT) whenever ANY of the following holds:
          * the arbiter call itself raised (network/runner error);
          * ``verdict.decision is None`` (empty/malformed/non-JSON response);
          * ``verdict.confidence is None``;
          * ``verdict.confidence < debate_config.confidence_threshold``;
          * ``verdict.decision`` is not ``"ACCEPT"`` (e.g. ``"DEFEND"``).

        Only an explicit, confident ``"ACCEPT"`` at/above the configured
        threshold resolves the challenge without a human.
        """
        from typing import cast

        from hivepilot.models import RunnerKind

        judge_def = RunnerDefinition(
            name="challenge:arbiter",
            kind=cast(RunnerKind, debate_config.runner),
            command=None,
            model=debate_config.model,
        )
        try:
            verdict = self._adjudicate_challenge(
                target_agent_name=target_agent_name,
                challenger_name=challenger_name,
                challenge_point=challenge_point,
                prior_output=prior_output,
                rebuttal_output=rebuttal_output,
                judge_def=judge_def,
                project=project,
                policy=policy,
                simulate=simulate,
            )
        except Exception as exc:  # never let a broken judge crash the pipeline
            logger.warning("challenge_arbiter.error", error=redact_text(str(exc)))
            # Sprint 3: a failed arbiter call must register as BLOCKING for the
            # fail-closed PR gate — never silently keep a prior approving
            # verdict governing (see `_register_verdict`'s "sticky" rule).
            failure_verdict = Verdict(decision=None, confidence=None)
            self._register_verdict(
                failure_verdict, confidence_threshold=debate_config.confidence_threshold
            )
            self._persist_challenge_verdict(
                run_id=run_id,
                project=project,
                target_agent_name=target_agent_name,
                challenger_name=challenger_name,
                verdict=failure_verdict,
            )
            return (
                True,
                f"MAINTAIN: Independent judge call failed ({redact_text(str(exc))[:200]}) — "
                "escalated to human review.",
            )

        accepted = (
            verdict.decision is not None
            and verdict.decision.strip().upper() == "ACCEPT"
            and verdict.confidence is not None
            and verdict.confidence >= debate_config.confidence_threshold
        )
        resolution_output = (
            f"{'ACCEPT' if accepted else 'MAINTAIN'}: independent judge verdict — "
            f"decision={verdict.decision!r}, confidence={verdict.confidence!r} "
            f"(threshold={debate_config.confidence_threshold})."
        )
        # Sprint 3: feed the fail-closed PR-gate aggregate + persist (redacted)
        # for later review — best-effort, never lets a persistence hiccup
        # break challenge resolution.
        self._register_verdict(verdict, confidence_threshold=debate_config.confidence_threshold)
        self._persist_challenge_verdict(
            run_id=run_id,
            project=project,
            target_agent_name=target_agent_name,
            challenger_name=challenger_name,
            verdict=verdict,
        )
        return not accepted, resolution_output

    def _persist_challenge_verdict(
        self,
        *,
        run_id: int | None,
        project: ProjectConfig,
        target_agent_name: str,
        challenger_name: str,
        verdict: "Verdict",
    ) -> None:
        """Best-effort persistence of a challenge-arbiter Verdict (Sprint 3).
        Never raises — a persistence hiccup must not break challenge
        resolution (see `_resolve_challenge_via_arbiter`'s call sites)."""
        try:
            state_service.record_verdict(
                run_id=run_id,
                project=project.path.name,
                task=None,
                role=target_agent_name,
                kind="challenge",
                decision=verdict.decision,
                confidence=verdict.confidence,
                summary=f"challenge from {challenger_name} against {target_agent_name}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("verdict.persist_failed", kind="challenge", error=str(exc))

    def run_pipeline(
        self,
        *,
        project_names: Iterable[str],
        pipeline_name: str,
        extra_prompt: str | None,
        auto_git: bool,
        concurrency: int | None = None,
        dry_run: bool = True,
        simulate: bool = False,
        start_index: int = 0,
        run_id: int | None = None,
        hub: str | None = None,
        components: list[str] | None = None,
        seed_context: str | None = None,
        group: Group | None = None,
    ) -> list[RunResult]:
        """Public entry point. Delegates to `_run_pipeline_body` inside a
        run-scope (see `_enter_run_scope`/`_exit_run_scope`) so the resolved-
        secrets masking registry is cleared once the WHOLE pipeline run —
        including every nested `run_task` call it makes per stage — is fully
        complete and every sink has already redacted against it.

        Also opens the root `pipeline.run` OTel span for the whole run —
        every `task.run`/`step.run` span opened by nested `run_task`/
        `_execute_task` calls (including across the `ThreadPoolExecutor`
        worker threads `_run_task_body` dispatches to — see
        `current_context`/`use_context`) nests under this one."""
        self._enter_run_scope()
        _project_names = list(project_names)
        _tracer = get_tracer()
        try:
            with _tracer.start_as_current_span(
                "pipeline.run",
                attributes={
                    "hivepilot.pipeline.name": pipeline_name,
                    "hivepilot.pipeline.projects": ",".join(_project_names),
                },
                record_exception=False,
                set_status_on_exception=False,
            ) as _pipeline_span:
                try:
                    result = self._run_pipeline_body(
                        project_names=_project_names,
                        pipeline_name=pipeline_name,
                        extra_prompt=extra_prompt,
                        auto_git=auto_git,
                        concurrency=concurrency,
                        dry_run=dry_run,
                        simulate=simulate,
                        start_index=start_index,
                        run_id=run_id,
                        hub=hub,
                        components=components,
                        seed_context=seed_context,
                        group=group,
                    )
                except Exception as exc:
                    record_exception_on_span(_pipeline_span, exc)
                    _pipeline_span.set_attribute("hivepilot.pipeline.status", "failed")
                    raise
                _pipeline_span.set_attribute("hivepilot.pipeline.status", "success")
                return result
        finally:
            self._exit_run_scope()

    def _run_pipeline_body(
        self,
        *,
        project_names: Iterable[str],
        pipeline_name: str,
        extra_prompt: str | None,
        auto_git: bool,
        concurrency: int | None = None,
        dry_run: bool = True,
        simulate: bool = False,
        start_index: int = 0,
        run_id: int | None = None,
        hub: str | None = None,
        components: list[str] | None = None,
        seed_context: str | None = None,
        group: Group | None = None,
    ) -> list[RunResult]:
        if pipeline_name not in self.pipelines.pipelines:
            raise ValueError(f"Unknown pipeline: {pipeline_name}")
        pipeline = self.pipelines.pipelines[pipeline_name]
        validate_pipeline(pipeline, self.tasks)

        # Fail-closed guard (plugin-arch-overhaul PRD, Sprint 01): a pipeline
        # with zero active agent runners (every built-in agent flag off and no
        # agent plugin registered) can never make progress, so refuse to start
        # before any stage executes. Local import: hivepilot.registry re-exports
        # AGENT_RUNNER_KINDS' single source of truth from
        # hivepilot.services.agent_checks, kept local here (rather than a
        # top-level import) purely to keep this narrow, self-contained edit
        # safe from the repo's PostToolUse formatter, which strips top-level
        # imports left unused across separate edits.
        from hivepilot.registry import NoAgentRunnerError, active_agent_runner_kinds
        from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS

        if not active_agent_runner_kinds():
            raise NoAgentRunnerError(
                "No agent runner is enabled. Enable at least one of: "
                f"{', '.join(sorted(AGENT_RUNNER_KINDS))} "
                "(e.g. set HIVEPILOT_CLAUDE_ENABLED=1)."
            )

        # Stage scoping (PRD A1): resolve the run's tag -> component map and
        # fail closed, up front, before any stage executes, if a stage
        # references a tag that isn't defined for this run's group. Applies
        # even when no `group` was passed (group_tags == {}): a stage that
        # declares `only_tags` without a group is an undefined-tag error too.
        group_tags: dict[str, list[str]] = group.tags if group is not None else {}
        _validate_stage_tags(pipeline.stages, group_tags)

        project_names = list(project_names)

        # Group mode: planning stages (before the checkpoint) run once in the hub;
        # execution stages (the pause_before stage onward) fan out over components.
        group_mode = bool(components)
        pause_index = next(
            (i for i, s in enumerate(pipeline.stages) if s.pause_before), len(pipeline.stages)
        )

        notification_service.stream_agent_turn(
            actor="HivePilot",
            stage=f"pipeline {pipeline_name}",
            summary=f"started on {', '.join(project_names)}",
            icon="🚀",
        )

        # Resolve vault path — None means artifact writes are silent no-ops
        vault_path = settings.obsidian_vault if settings.obsidian_vault.exists() else None

        # Open (or resume) a state-service run record (RUNNING)
        if run_id is None:
            run_id = state_service.record_run_start(
                pipeline_name,
                pipeline_name,
                status=RunStatus.RUNNING.value,
            )

        interactions_svc = InteractionService(vault_path, dry_run=dry_run)
        notification_service.emit_event(
            "pipeline_start", run_id=run_id, pipeline=pipeline_name, projects=project_names
        )
        try:
            self.plugins.run_hook(
                "on_pipeline_start", run_id=run_id, pipeline=pipeline_name, projects=project_names
            )
        except Exception as exc:  # noqa: BLE001 — a broken plugin hook must not kill a run
            logger.warning(
                "plugins.hook_failed", hook="on_pipeline_start", run_id=run_id, error=str(exc)
            )

        results: list[RunResult] = []
        final_status = RunStatus.COMPLETE
        prior_chunks: list[str] = []  # outputs of completed stages, fed to later agents
        # PRD A2 Sprint 1: run-scoped keyed store (output-key -> content), populated
        # alongside prior_chunks below via section-extraction with whole-blob coarse
        # fallback. Built but NOT consumed anywhere yet — inert this sprint.
        outputs_by_key: dict[str, str] = {}
        _request_budget: dict[str, int] = {"remaining": settings.max_requests_per_run}
        if seed_context:
            prior_chunks.append(seed_context)
        elif group_mode:
            prior_chunks.append(
                "Components of this product (decide which ones this change should touch):\n"
                + "\n".join(f"- {c}" for c in (components or []))
                + "\n\nWhen you synthesize the plan, end with a line listing the impacted "
                "components exactly as: `COMPONENTS: name1, name2` (using the names above)."
            )
        selected_components = list(components or [])  # narrowed by the agents' COMPONENTS line
        for stage_idx, stage in enumerate(pipeline.stages):
            if stage_idx < start_index:
                continue  # already executed before the checkpoint pause

            # Group mode: the agents pick which components the change touches.
            if group_mode and stage_idx == pause_index:
                picked = _parse_components("\n\n".join(prior_chunks), components or [])
                if picked:
                    selected_components = picked

            # Stage scoping (PRD A1): skip this stage entirely when it declares
            # only_components/only_tags and that target is disjoint from the
            # components selected for this run. The stage's task is never
            # invoked, `prior_chunks`/artifacts/interaction-log are left
            # untouched (early continue), and the run carries on. A single
            # RunResult with skipped=True is appended so the skip is
            # distinguishable in the run record from both success and
            # failure (success=True, skipped=True — never counted as a
            # failure, never confused with a real completed stage).
            if _stage_should_skip(stage, group_tags, selected_components):
                target_components = sorted(_resolve_stage_target_components(stage, group_tags))
                logger.info(
                    "pipeline.stage_skipped",
                    pipeline=pipeline_name,
                    stage=stage.name,
                    only_components=stage.only_components,
                    only_tags=stage.only_tags,
                    selected_components=selected_components,
                )
                results.append(
                    RunResult(
                        project=", ".join(selected_components)
                        or (project_names[0] if project_names else pipeline_name),
                        target=f"{pipeline_name}:{stage.name}",
                        success=True,
                        detail=(
                            f"skipped: scoped to {target_components}, disjoint from "
                            f"selected components {selected_components}"
                        ),
                        skipped=True,
                    )
                )
                continue

            # Plan checkpoint: pause for human approval before this stage.
            # Skipped under --simulate, consistent with simulate bypassing approvals.
            if stage.pause_before and stage_idx != start_index and not simulate:
                completed = [s.name for s in pipeline.stages[:stage_idx]]
                checkpoint_meta = {
                    "kind": "pipeline_checkpoint",
                    "pipeline": pipeline_name,
                    "projects": project_names,
                    "resume_from_index": stage_idx,
                    "extra_prompt": extra_prompt,
                    "auto_git": auto_git,
                    "dry_run": dry_run,
                    "simulate": simulate,
                    "next_stage": stage.name,
                    "completed_stages": completed,
                    "hub": hub,
                    "components": selected_components,
                    "planning_context": "\n\n".join(prior_chunks) or None,
                }
                state_service.record_approval_request(
                    run_id,
                    project_names[0] if project_names else pipeline_name,
                    pipeline_name,
                    checkpoint_meta,
                )
                notification_service.send_approval_keyboard(
                    run_id=run_id,
                    project=", ".join(project_names) or pipeline_name,
                    task=f"plan → {stage.name}",
                    details=_build_checkpoint_details(
                        prior_chunks=prior_chunks,
                        completed=completed,
                        next_stage=stage.name,
                        components=selected_components,
                        group_mode=group_mode,
                    ),
                )
                proposal_excerpt = (prior_chunks[-1] if prior_chunks else "").strip()
                notification_service.stream_agent_turn(
                    actor="HivePilot",
                    stage="checkpoint",
                    summary=(
                        f"Plan ready ({', '.join(completed)}). "
                        + (
                            f"Target components: {', '.join(selected_components)}. "
                            if group_mode
                            else ""
                        )
                        + f'Approve (run #{run_id}) to start "{stage.name}". '
                        + "Full plan in the Obsidian vault."
                        + (f"\n\n{proposal_excerpt}" if proposal_excerpt else "")
                    ),
                    icon="⏸️",
                )
                notification_service.emit_event(
                    "checkpoint",
                    run_id=run_id,
                    pipeline=pipeline_name,
                    next_stage=stage.name,
                    components=selected_components if group_mode else None,
                    status="awaiting_approval",
                )
                state_service.complete_run(run_id, RunStatus.PAUSED.value)
                return results

            if group_mode:
                if group is not None and group.single_repo:
                    # Monorepo group (single_repo): components/tags are pure
                    # scoping labels (already applied above via
                    # _stage_should_skip) — every stage that runs, runs ONCE at
                    # the hub (the monorepo root), never fanned out per
                    # component, and git ops run there too since the hub IS
                    # the code repo in this mode (unlike the multi-repo hub,
                    # which is a planning-only product/parent dir).
                    if not hub:
                        raise ValueError(
                            "single_repo group requires a non-empty 'hub' "
                            "(should have been rejected at config-load time)"
                        )
                    targets = [hub]
                    stage_auto_git = auto_git
                else:
                    is_hub_stage = bool(stage_idx < pause_index and hub)
                    targets = [hub] if is_hub_stage else selected_components
                    # Planning stages run on the hub, which is the product/parent dir
                    # (not a code git repo). Code git actions only make sense on the
                    # component repos in the post-checkpoint fan-out; the hub's planning
                    # artifacts are persisted via the vault auto-commit instead.
                    stage_auto_git = auto_git and not is_hub_stage
            else:
                targets = project_names
                stage_auto_git = auto_git
            # PRD A2 Sprint 2: resolve the CONSUMING stage's role (the stage
            # about to run) to decide whether prior_context is routed from the
            # keyed store or built the classic way. Gated on
            # settings.context_routing_mode only — see _route_prior_context.
            from hivepilot.roles import ROLES

            consuming_task = self.tasks.tasks.get(stage.task)
            consuming_role = (
                ROLES.get(consuming_task.role) if consuming_task and consuming_task.role else None
            )
            stage_results = self.run_task(
                project_names=targets,
                task_name=stage.task,
                extra_prompt=extra_prompt,
                auto_git=stage_auto_git,
                concurrency=concurrency,
                simulate=simulate,
                dry_run=dry_run,
                # Resolve the stage's execution mode (stage over pipeline over
                # "cli" default) and propagate it into every step's dispatch.
                mode=resolve_mode(pipeline, stage),
                stage_skills=list(stage.skills) if stage.skills else None,
                # Resolve the stage's model/effort defaults (stage over
                # pipeline, None otherwise) and propagate them into every
                # role-driven step's dispatch (see `resolve_stage_dispatch`,
                # which layers a policy `role_overrides` entry OVER these).
                stage_model=resolve_stage_model(pipeline, stage),
                stage_effort=resolve_effort(pipeline, stage),
                prior_context=_route_prior_context(
                    role=consuming_role,
                    prior_chunks=prior_chunks,
                    outputs_by_key=outputs_by_key,
                    routing_mode=settings.context_routing_mode,
                    prior_context_mode=settings.prior_context_mode,
                    max_chars=settings.max_prior_context_chars,
                    stage_name=stage.name,
                ),
                # debate-judge-pipeline-yaml PRD, Sprint 2: thread the raw
                # stage/pipeline through to `_execute_task_body`'s
                # `_effective_debate` resolution (dual-model-debate judge +
                # fail-closed verdict gate).
                stage=stage,
                pipeline=pipeline,
            )
            results.extend(
                [
                    RunResult(r.project, f"{pipeline_name}:{stage.name}", r.success, r.detail)
                    for r in stage_results
                ]
            )

            # Aggregate stage output for the vault artifact
            stage_output = "\n".join(
                r.detail or f"{r.project}: {'ok' if r.success else 'failed'}" for r in stage_results
            )

            # PRD A2 Sprint 1/2: populate the run-scoped keyed store alongside
            # prior_chunks. Same-key entries from a later stage overwrite earlier
            # ones (last producer wins). Consumed by _route_prior_context above
            # for the NEXT stage's turn, but ONLY in context_routing_mode="keyed"
            # (Sprint 2) — in "full" mode (default) this dict is populated but
            # never read, so the prior_chunks/build_prior_context path remains
            # byte-identical to pre-Sprint-2 behaviour.
            from hivepilot.roles import ROLES

            producing_task = self.tasks.tasks.get(stage.task)
            producing_role = (
                ROLES.get(producing_task.role) if producing_task and producing_task.role else None
            )
            if producing_role and producing_role.outputs:
                outputs_by_key.update(_stage_outputs_by_key(stage_output, producing_role.outputs))

            prior_chunks.append(f"## {self._agent_name(stage)} ({stage.name})\n{stage_output}")
            write_stage_artifact(
                vault_path=vault_path,
                run_id=run_id,
                stage_name=stage.name,
                output=stage_output,
                dry_run=dry_run,
            )

            # Per-stage interaction log (2.6a)
            next_stages = pipeline.stages[stage_idx + 1 :]
            next_target = self._agent_name(next_stages[0]) if next_stages else None
            from datetime import datetime, timezone

            interactions_svc.log_interaction(
                Interaction(
                    actor=self._agent_name(stage),
                    action="completed stage",
                    target=next_target,
                    summary=stage_output,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    run_id=run_id,
                    metadata={"pipeline": pipeline_name, "stage_index": stage_idx},
                )
            )
            notification_service.stream_agent_turn(
                actor=self._agent_name(stage),
                stage=stage.name,
                target=next_target,
                summary=stage_output,
            )

            # Surface inter-agent challenges (⚔️) and bounded rebuttal (🛡️/⚖️/🙋)
            _report = parse_agent_report(stage_output)
            if _report.challenge:
                _challenger_name = self._agent_name(stage)
                _challenge_target = _report.challenge.target
                _challenge_point = _report.challenge.point
                notification_service.stream_challenge(
                    actor=_challenger_name,
                    target=_challenge_target,
                    point=_challenge_point,
                )
                if _METRICS_AVAILABLE and _metrics is not None:
                    try:
                        _metrics.challenges_total.inc()
                    except Exception:  # noqa: BLE001
                        pass
                log_challenge_interaction(
                    actor=_challenger_name,
                    target=_challenge_target,
                    point=_challenge_point,
                )

                # Bounded rebuttal round: target defends → challenger accepts/maintains
                if settings.enable_challenge_rounds and settings.max_challenge_rounds >= 1:
                    try:
                        _rebuttal_policy = policy_service.get_policy(
                            project_names[0] if project_names else pipeline_name
                        )
                        _rebuttal_project_name = (
                            project_names[0] if project_names else pipeline_name
                        )
                        self._run_rebuttal_round(
                            challenger_name=_challenger_name,
                            challenge_target=_challenge_target,
                            challenge_point=_challenge_point,
                            challenger_stage=stage,
                            completed_stages=pipeline.stages[:stage_idx],
                            prior_chunks=prior_chunks,
                            policy=_rebuttal_policy,
                            project_name=_rebuttal_project_name,
                            simulate=simulate,
                            run_id=run_id,
                            pipeline=pipeline,
                        )
                    except Exception as _rebuttal_exc:  # noqa: BLE001
                        logger.warning(
                            "rebuttal.error",
                            challenger=_challenger_name,
                            target=_challenge_target,
                            error=str(_rebuttal_exc),
                        )

            # Tier-2: on-demand agent-to-agent requests (❓/↩️)
            if settings.enable_agent_requests:
                _req_policy = policy_service.get_policy(
                    project_names[0] if project_names else pipeline_name
                )
                stage_output = self._handle_agent_requests(
                    stage_output=stage_output,
                    actor=self._agent_name(stage),
                    stage=stage,
                    project_names=list(project_names),
                    policy=_req_policy,
                    budget=_request_budget,
                )

            # Documentation vault changelog note (2.6c)
            if stage.commits_vault and vault_path is not None:
                doc_svc = ObsidianService(vault_path, dry_run=dry_run)
                doc_svc.write_note(
                    subpath=f"Docs/changelog-run-{run_id}.md",
                    title=f"Documentation update — pipeline run {run_id}",
                    body=stage_output,
                    frontmatter_fields={
                        "type": "documentation",
                        "run_id": run_id,
                        "pipeline": pipeline_name,
                        "agent": "gemini-cli",
                        "stage": stage.name,
                    },
                )

            # Commit+push the vault per stage so notes land in Obsidian as they're
            # written (not just at run end). Opt-in, best-effort.
            if (
                settings.auto_commit_vault
                and not simulate
                and not dry_run
                and vault_path is not None
            ):
                try:
                    from hivepilot.services.git_service import commit_vault

                    commit_vault(
                        vault_path, f"HivePilot: {pipeline_name} run {run_id} — {stage.name}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "vault.commit_failed", run_id=run_id, stage=stage.name, error=str(exc)
                    )

            stage_failed = any(not r.success for r in stage_results)
            if stage_failed and not stage.continue_on_failure:
                logger.warning(
                    "pipeline.fail_fast",
                    pipeline=pipeline_name,
                    stage=stage.name,
                    remaining=[s.name for s in next_stages],
                )
                final_status = RunStatus.TEST_FAILURE
                try:
                    self.plugins.run_hook(
                        "on_error",
                        run_id=run_id,
                        pipeline=pipeline_name,
                        stage=stage.name,
                        dry_run=dry_run,
                    )
                except Exception as exc:  # noqa: BLE001 — a broken plugin hook must not kill a run
                    logger.warning(
                        "plugins.hook_failed", hook="on_error", run_id=run_id, error=str(exc)
                    )
                break

        state_service.complete_run(run_id, final_status.value)
        notification_service.emit_event(
            "complete", run_id=run_id, pipeline=pipeline_name, status=final_status.value
        )
        try:
            self.plugins.run_hook(
                "on_pipeline_end",
                run_id=run_id,
                pipeline=pipeline_name,
                status=final_status.value,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — a broken plugin hook must not kill a run
            logger.warning(
                "plugins.hook_failed", hook="on_pipeline_end", run_id=run_id, error=str(exc)
            )

        # Version the plan/ADR notes: commit+push the Obsidian vault (best-effort,
        # opt-in, only on a real write run).
        if settings.auto_commit_vault and not simulate and not dry_run and vault_path is not None:
            try:
                from hivepilot.services.git_service import commit_vault

                commit_vault(vault_path, f"HivePilot: {pipeline_name} run {run_id}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("vault.commit_failed", run_id=run_id, error=str(exc))

        # Henri (external auditor) observes the completed cycle — best-effort, never
        # breaks a run; skipped under --simulate and when disabled.
        if settings.auditor_auto and not simulate and project_names:
            try:
                from hivepilot.services import auditor_service

                auditor_service.observe(
                    project=self._project(project_names[0]),
                    run_id=run_id,
                    registry=self.registry,
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("auditor.observe_failed", run_id=run_id, error=str(exc))
        return results

    def resume_pipeline(
        self,
        *,
        run_id: int,
        approve: bool,
        approver: str,
        auto_git: bool | None = None,
    ) -> RunResult:
        """Resume (or deny) a pipeline parked at a plan checkpoint.

        On approve, continue ``run_pipeline`` from the saved resume point under the
        same ``run_id``; on deny, mark the run denied and run nothing further.
        ``auto_git`` (when not None) overrides the run's stored auto_git — e.g. to
        enable push/PR at approval time on a run launched without ``--auto-git``.
        """
        approval = state_service.get_approval(run_id)
        if not approval or approval.get("status") != "pending":
            raise ValueError(f"Run {run_id} is not a pending checkpoint.")
        meta = json.loads(approval.get("metadata") or "{}")
        if meta.get("kind") != "pipeline_checkpoint":
            raise ValueError(f"Run {run_id} is not a pipeline checkpoint.")
        pipeline_name = meta["pipeline"]

        if not approve:
            state_service.update_approval(run_id, "denied", approver)
            state_service.complete_run(run_id, "denied", "Plan denied at checkpoint")
            notification_service.send_notification(
                f"❌ Plan #{run_id} ({pipeline_name}) denied — pipeline stopped."
            )
            notification_service.emit_event(
                "denied", run_id=run_id, pipeline=pipeline_name, approver=approver
            )
            return RunResult(pipeline_name, pipeline_name, False, "Plan denied at checkpoint")

        state_service.update_approval(run_id, "approved", approver)
        notification_service.send_notification(
            f"✅ Plan #{run_id} ({pipeline_name}) approved — starting development."
        )
        notification_service.emit_event(
            "approved", run_id=run_id, pipeline=pipeline_name, approver=approver
        )
        results = self.run_pipeline(
            project_names=meta["projects"],
            pipeline_name=pipeline_name,
            extra_prompt=meta.get("extra_prompt"),
            auto_git=auto_git if auto_git is not None else meta.get("auto_git", False),
            dry_run=meta.get("dry_run", True),
            simulate=meta.get("simulate", False),
            start_index=meta["resume_from_index"],
            run_id=run_id,
            hub=meta.get("hub"),
            components=meta.get("components"),
            seed_context=meta.get("planning_context"),
        )
        ok = all(r.success for r in results)
        return RunResult(pipeline_name, pipeline_name, ok)

    def human_challenge(self, run_id: int, challenge_text: str, approver: str) -> str:
        """Process a human challenge/question at a plan checkpoint.

        The run stays paused — caller must not call resume_pipeline.
        Returns the CoS response string.
        """
        import json as _json
        from typing import cast

        from hivepilot.models import RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_stage_dispatch
        from hivepilot.runners.base import RunnerPayload

        row = state_service.get_approval(run_id)
        if not row:
            raise ValueError(f"No approval row found for run_id={run_id}")

        raw_meta = row.get("metadata") or "{}"
        meta: dict = _json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        planning_context = meta.get("planning_context", "")
        project_name = row.get("project", "unknown")

        # Resolve CoS role (Jules)
        cos_role_key = "cos"
        try:
            cos_role = get_role(cos_role_key)
        except Exception:
            cos_role = None

        # Get policy for this project
        try:
            policy = policy_service.get_policy(project_name)
        except Exception:
            policy = None

        if cos_role is not None:
            # No pipeline stage exists at this out-of-band, paused-approval
            # entry point — resolves identically to `resolve_runner`.
            runner_kind, role_model, hc_effort = resolve_stage_dispatch(cos_role_key, policy)
            role_perm = cos_role.permission_mode
            role_options: dict[str, str] = {}
            if role_perm:
                role_options["permission_mode"] = role_perm
            runner_def = RunnerDefinition(
                name="human_challenge:cos",
                kind=cast(RunnerKind, runner_kind),
                command=None,
                model=role_model,
                effort=hc_effort,
                host=resolve_host(cos_role_key, policy),
                options=role_options,
            )
            prompt_file = (
                str(cos_role.prompt_file)
                if cos_role.prompt_file and cos_role.prompt_file.exists()
                else ""
            )
            step = TaskStep(
                name="human_challenge:cos",
                runner=runner_kind,
                prompt_file=prompt_file,
            )
        else:
            # Fallback: use default claude runner
            runner_def = RunnerDefinition(
                name="human_challenge:cos",
                kind=cast(RunnerKind, "claude"),
                command=None,
                model=None,
                host=None,
                options={},
            )
            step = TaskStep(
                name="human_challenge:cos",
                runner="claude",
                prompt_file="",
            )

        challenge_prompt = (
            "You are Jules, the Chief of Staff. A human is challenging/asking about a paused"
            " pipeline plan.\n\n"
            f"CURRENT PLAN CONTEXT:\n{planning_context}\n\n"
            f"HUMAN CHALLENGE/QUESTION: {challenge_text}\n\n"
            "Respond with your analysis and any plan revisions needed."
            " Keep your response concise (under 400 words)."
        )
        payload = RunnerPayload(
            project_name=project_name,
            project=None,
            task_name="human_challenge:cos",
            step=step,
            metadata={"extra_prompt": challenge_prompt, "prior_context": ""},
            secrets={},
        )

        answer = self.registry.capture_definition(runner_def, payload)

        # Log interactions
        try:
            log_challenge_interaction(actor=approver, target="Jules (CoS)", point=challenge_text)
            log_challenge_interaction(actor="Jules (CoS)", target=approver, point=answer)
        except Exception as exc:  # noqa: BLE001
            logger.warning("human_challenge.log_failed", error=str(exc))

        # Persist challenge + response into planning_context
        meta["planning_context"] = (
            planning_context
            + f"\n\n[HUMAN CHALLENGE by {approver}]: {challenge_text}\n[JULES RESPONSE]: {answer}"
        )
        state_service.update_approval_metadata(run_id, meta)

        return answer

    def run_approved(
        self,
        *,
        run_id: int,
        approve: bool,
        approver: str,
        reason: str | None = None,
    ) -> RunResult:
        approval = state_service.get_approval(run_id)
        if not approval or approval["status"] != "pending":
            raise ValueError(f"Run {run_id} is not pending approval.")
        project_name = approval["project"]
        task_name = approval["task"]
        metadata = json.loads(approval.get("metadata") or "{}")

        if not approve:
            state_service.update_approval(run_id, "denied", approver)
            state_service.complete_run(run_id, "denied", reason or "Denied by approval workflow")
            notification_service.send_notification(
                f"❌ Run {run_id} for {project_name}:{task_name} denied by {approver}"
            )
            return RunResult(project_name, task_name, False, "Denied")

        policy = policy_service.get_policy(project_name)
        project = self._project(project_name)
        task = self.tasks.tasks[task_name]
        state_service.update_approval(run_id, "approved", approver)
        notification_service.send_notification(
            f"✅ Run {run_id} for {project_name}:{task_name} approved by {approver}."
        )
        # Step-level checkpoint (Phase 17a-B): resume the task FROM the
        # paused step — mirrors resume_pipeline's `start_index`, one level
        # finer. Prior steps are never re-executed (resume_from_step skips
        # them) and their accumulated output is restored (resume_outputs).
        # `approved_step_index` marks the paused step as already-approved so
        # `_execute_task` doesn't immediately re-gate it. Non-step-checkpoint
        # approvals (the existing per-task `policy.require_approval` path)
        # get the same defaults `_execute_task` always had (start from step 0,
        # nothing pre-approved) — byte-identical to before this sprint.
        _is_step_checkpoint = metadata.get("kind") == "step_checkpoint"
        _resume_from_step = metadata.get("resume_from_step", 0) if _is_step_checkpoint else 0
        _resume_outputs = metadata.get("resume_outputs") if _is_step_checkpoint else None
        _approved_step_index = _resume_from_step if _is_step_checkpoint else None
        _dry_run = metadata.get("dry_run", True) if _is_step_checkpoint else True
        _stage_skills = metadata.get("stage_skills") if _is_step_checkpoint else None

        # Phase 21 Sprint 3 -- CVE gate defense-in-depth. `require_approval`
        # and `block_on_severity` are independent gates in `_run_task_body`
        # (an `if`/`elif`, not both): a project configured with BOTH only
        # ever has the `require_approval` branch evaluated pre-run, so the
        # CVE gate is never checked before this approved run dispatches to
        # `_execute_task` directly. Without this check an approver could
        # approve straight past a critical CVE finding. Only applies to the
        # per-task `require_approval` resume (`_is_step_checkpoint` False):
        # a step-checkpoint resume is a LATER pause of a run that already
        # passed through this exact check on its first, non-step-checkpoint
        # `run_approved` call for the same `run_id` -- re-running it here
        # would be a redundant (but harmless) second scan, so we skip it.
        # `simulate` is never in play here: `_run_task_body` only enters the
        # `require_approval` branch (the source of this resume) when
        # `not simulate`, so a simulated run never produces a pending
        # approval for `run_approved` to resume.
        if not _is_step_checkpoint and policy and policy.block_on_severity:
            cve_block_detail = self._cve_gate_block_detail(
                project, policy.scan_tool, policy.block_on_severity
            )
            if cve_block_detail is not None:
                state_service.complete_run(run_id, "failed", cve_block_detail)
                notification_service.send_notification(
                    f"⛔ {project_name}: {task_name} blocked by CVE gate"
                )
                return RunResult(project_name, task_name, False, cve_block_detail)

        try:
            self._execute_task(
                project=project,
                task_name=task_name,
                task=task,
                extra_prompt=metadata.get("extra_prompt"),
                auto_git=metadata.get("auto_git", False),
                run_id=run_id,
                policy=policy,
                dry_run=_dry_run,
                resume_from_step=_resume_from_step,
                resume_outputs=_resume_outputs,
                approved_step_index=_approved_step_index,
                stage_skills=_stage_skills,
            )
        except StepApprovalPending as exc:
            # A LATER step in the same task also requires approval —
            # `_execute_task` already recorded a fresh approval request and
            # marked the run PAUSED again before raising. Not a failure.
            logger.info("run_approved.step_paused_again", run_id=run_id, error=str(exc))
            return RunResult(project_name, task_name, False, f"Pending approval: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error("run_approved.failure", run_id=run_id, error=str(exc))
            state_service.complete_run(run_id, "failed", str(exc))
            notification_service.send_notification(
                f"❌ Run {run_id} for {project_name}:{task_name} failed after approval ({exc})"
            )
            # Phase 10c: same choke-point rationale as `_run_task_body` — mask
            # registered secrets before this reaches cli/api/chat sinks.
            return RunResult(project_name, task_name, False, redact_text(str(exc)))
        state_service.complete_run(run_id, "success")
        return RunResult(project_name, task_name, True)

    def run_debate(
        self,
        *,
        project_name: str,
        role_name: str,
        topic: str,
        dry_run: bool = True,
        simulate: bool = False,
        prior_context: str | None = None,
        debate_config: "EffectiveDebateConfig | None" = None,
        run_id: int | None = None,
        task_name: str | None = None,
    ) -> dict | None:
        """Public entry point. Delegates to `_run_debate_body` inside a
        run-scope (see `_enter_run_scope`/`_exit_run_scope`) so the resolved-
        secrets masking registry is cleared once this debate call completes.
        Standalone debates are triggered repeatedly via ChatOps in the daemon
        (see cli.py), so — same as run_task/run_pipeline — this call must not
        leak resolved secret values into the registry across invocations. When
        nested inside a role-driven `run_task` call, the shared depth counter
        means this inner scope's exit does NOT clear prematurely — the outer
        run_task's own sinks still see the registry populated.

        ``debate_config`` is the caller's already-resolved
        `EffectiveDebateConfig` (debate-judge-pipeline-yaml PRD, Sprint 2 —
        see `_effective_debate`), e.g. `_execute_task_body`'s pipeline/stage
        resolution for a role-driven dual-model debate task. `None` (a
        standalone `run_debate` call, e.g. cli.py's `debate` command) resolves
        to the floor only inside `_run_debate_body`, byte-identical to pre-
        Sprint-2 behaviour.

        ``run_id``/``task_name`` (auto-learning-lessons-loop PRD, Sprint 1)
        are the enclosing run's real id + task name, when this debate is
        role-driven from inside `_execute_task_body` (mirrors how
        `_resolve_challenge_via_arbiter` already threads `run_id` into
        `_persist_challenge_verdict` for the challenge-arbiter path). Both
        default to `None` for a standalone `run_debate` call (cli.py's
        `debate` command / the ChatOps daemon), which persists the judge
        verdict with `run_id=None` exactly as before — byte-identical for
        callers outside a run context."""
        self._enter_run_scope()
        try:
            return self._run_debate_body(
                project_name=project_name,
                role_name=role_name,
                topic=topic,
                dry_run=dry_run,
                simulate=simulate,
                prior_context=prior_context,
                debate_config=debate_config,
                run_id=run_id,
                task_name=task_name,
            )
        finally:
            self._exit_run_scope()

    def _adjudicate(
        self,
        positions: list[Position],
        judge_def: RunnerDefinition,
        *,
        role_name: str,
        topic: str,
        project: ProjectConfig,
        policy: policy_service.Policy | None,
        simulate: bool = False,
    ) -> Verdict:
        """Synthesize *positions* into a single :class:`Verdict` via ONE judge
        `capture_definition` call (Debate Judge & Consensus PRD, Sprint 1).

        STABLE contract reused by Sprint 2 — see the module-level `Verdict` /
        `_parse_verdict` docstrings for the exact shape and parse rules.

        Reuses `_resolve_secrets` (same pattern as each brain call in
        `_run_debate_body`) so the judge call inherits the SAME secret-masking
        scope as the rest of the debate run. The judge's raw output is passed
        through `redact_text` before parsing, so any leaked secret value is
        masked before it can reach the decision/confidence/per_role_stance
        that ends up in the ADR.

        When *simulate* is True, short-circuits to a deterministic synthetic
        verdict WITHOUT a real runner call — mirrors how each brain position
        is synthesized under `simulate` in `_run_debate_body`.

        Never fabricates a decision: a malformed/empty judge response returns
        `Verdict(decision=None, confidence=None)`, which the caller MUST treat
        as "no confident decision" (fall back to the templated/majority path).
        """
        prompt = _build_judge_prompt(topic, positions)
        step = TaskStep(
            name=f"{role_name}-judge",
            runner=judge_def.kind,
            prompt_file=None,
        )
        payload = RunnerPayload(
            project_name=project.path.name,
            project=project,
            task_name=f"debate:{role_name}:judge",
            step=step,
            metadata={"extra_prompt": prompt, "prior_context": ""},
            secrets=self._resolve_secrets(step, project, policy),
        )
        if simulate:
            raw = f'{{"decision": "[simulated judge decision for: {topic}]", "confidence": 0.5}}'
        else:
            raw = self.registry.capture_definition(judge_def, payload)
        raw = redact_text(raw) if raw else raw
        return _parse_verdict(raw or "")

    def _adjudicate_challenge(
        self,
        *,
        target_agent_name: str,
        challenger_name: str,
        challenge_point: str,
        prior_output: str,
        rebuttal_output: str,
        judge_def: RunnerDefinition,
        project: ProjectConfig,
        policy: policy_service.Policy | None,
        simulate: bool = False,
    ) -> Verdict:
        """Adjudicate a challenge/rebuttal pair into a single :class:`Verdict`
        via ONE INDEPENDENT judge `capture_definition` call (Debate Judge &
        Consensus PRD, Sprint 2).

        The judge is a THIRD role — never *challenger_name*, never
        *target_agent_name* — so the ACCEPT/DEFEND resolution is never
        self-graded by either party. Reuses the Sprint 1 `Verdict` /
        `_parse_verdict` contract as-is (see `_adjudicate`'s docstring).

        Reuses `_resolve_secrets` (same pattern as `_adjudicate`) so the judge
        call inherits the SAME secret-masking scope as the rest of the run —
        the judge's raw output is passed through `redact_text` before parsing,
        so any leaked secret value is masked before it can reach the verdict
        that gets logged/persisted (Judge Reuses Secret Scope invariant).

        When *simulate* is True, short-circuits to a deterministic synthetic
        ACCEPT verdict WITHOUT a real runner call — mirrors `_adjudicate`'s
        simulate branch.

        Never fabricates a decision: a malformed/empty judge response returns
        `Verdict(decision=None, confidence=None)`, which the caller MUST treat
        as "no confident decision" and escalate to human review — a judge
        error must fail TOWARD a human, never toward a silent ACCEPT.
        """
        prompt = _build_challenge_arbiter_prompt(
            target_name=target_agent_name,
            challenger_name=challenger_name,
            challenge_point=challenge_point,
            prior_output=prior_output,
            rebuttal_output=rebuttal_output,
        )
        step = TaskStep(
            name="challenge-arbiter",
            runner=judge_def.kind,
            prompt_file=None,
        )
        payload = RunnerPayload(
            project_name=project.path.name,
            project=project,
            task_name="challenge:arbiter",
            step=step,
            metadata={"extra_prompt": prompt, "prior_context": ""},
            secrets=self._resolve_secrets(step, project, policy),
        )
        if simulate:
            raw = '{"decision": "ACCEPT", "confidence": 0.9}'
        else:
            raw = self.registry.capture_definition(judge_def, payload)
        raw = redact_text(raw) if raw else raw
        return _parse_verdict(raw or "")

    def _run_debate_body(
        self,
        *,
        project_name: str,
        role_name: str,
        topic: str,
        dry_run: bool = True,
        simulate: bool = False,
        prior_context: str | None = None,
        debate_config: "EffectiveDebateConfig | None" = None,
        run_id: int | None = None,
        task_name: str | None = None,
    ) -> dict | None:
        """Run a dual-model debate for *role_name*: capture each model's position,
        synthesize via DebateService, and write an ADR. Returns the ADR emit dict.

        ``debate_config`` — see `run_debate`'s docstring. `None` resolves to
        the floor only via `_effective_debate(None, None)`.

        ``run_id``/``task_name`` — see `run_debate`'s docstring; threaded
        into the judge `record_verdict(...)` call below so a role-driven
        debate's verdict correlates to the run/task that produced it,
        instead of always persisting `run_id=None`."""
        from typing import cast

        from hivepilot.models import RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_runner
        from hivepilot.services.debate_service import DebateService, Position

        effective = (
            debate_config if debate_config is not None else self._effective_debate(None, None)
        )
        role = get_role(role_name)
        models = list(role.models or ([role.model] if role.model else []))
        if len(models) < 2:
            raise ValueError(
                f"Role '{role_name}' is not a dual-model debate role (models={models})."
            )
        project = self._project(project_name)
        policy = policy_service.get_policy(project_name)
        runner_kind, _, role_effort = resolve_runner(
            role_name, policy
        )  # also enforces allowed_runners
        debate_host = resolve_host(role_name, policy)
        vault_path = settings.obsidian_vault if settings.obsidian_vault.exists() else None

        positions: list[Position] = []
        for entry in models:
            # A brain may pin its own runner via "runner:model"
            # (e.g. "claude:claude-sonnet-4-6"); a bare model uses the role's runner.
            brain_runner, brain_model = _parse_brain(entry, runner_kind)
            step = TaskStep(
                name=f"{role_name}-{brain_model}",
                runner=brain_runner,
                prompt_file=str(role.prompt_file),
            )
            payload = RunnerPayload(
                project_name=project.path.name,
                project=project,
                task_name=f"debate:{role_name}",
                step=step,
                # The debate topic IS the brief / hand-off context for this role;
                # inject it as extra_prompt so each brain actually sees it (otherwise
                # e.g. the CEO gets no brief and correctly returns NEEDS_HUMAN).
                metadata={"extra_prompt": topic, "prior_context": prior_context or ""},
                secrets=self._resolve_secrets(step, project, policy),
            )
            if simulate:
                output = f"[simulated {brain_model} position on: {topic}]"
            else:
                rdef = RunnerDefinition(
                    name=f"debate:{role_name}:{brain_model}",
                    kind=cast(RunnerKind, brain_runner),
                    command=None,
                    model=brain_model,
                    effort=role_effort,
                    host=debate_host,
                )
                output = self.registry.capture_definition(rdef, payload)
            positions.append(
                Position(
                    role=f"{role_name}:{brain_model}",
                    stance="proposal",
                    rationale=output.strip()[:1000],
                )
            )
            notification_service.stream_agent_turn(
                actor=f"{role.display_name or role_name} ({role.title}) · {brain_model}",
                stage="debate",
                summary=output,
                icon="💬",
            )

        decision = (
            f"Synthesis of {len(models)} model proposals ({', '.join(models)}) for: {topic}. "
            f"Each model's proposal is recorded; final arbitration by {role_name} / human review."
        )
        confidence: float | None = None
        if effective.enable_judge:
            judge_def = RunnerDefinition(
                name=f"debate:{role_name}:judge",
                kind=cast(RunnerKind, effective.runner),
                command=None,
                model=effective.model,
                effort=role_effort,
                host=debate_host,
            )
            verdict = self._adjudicate(
                positions,
                judge_def,
                role_name=role_name,
                topic=topic,
                project=project,
                policy=policy,
                simulate=simulate,
            )
            # Never fabricate: only override the templated decision when the
            # judge produced a confident one (see `_parse_verdict` parse rules).
            if verdict.decision is not None:
                decision = verdict.decision
                confidence = verdict.confidence
            # Sprint 3: feed the fail-closed PR-gate aggregate (see
            # `_register_verdict`) and persist (redacted) for later review —
            # best-effort, never lets a persistence hiccup break the debate.
            self._register_verdict(verdict, confidence_threshold=effective.confidence_threshold)
            try:
                state_service.record_verdict(
                    run_id=run_id,
                    project=project.path.name,
                    task=task_name,
                    role=role_name,
                    kind="debate",
                    decision=verdict.decision,
                    confidence=verdict.confidence,
                    summary=f"debate judge synthesis for: {topic}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("verdict.persist_failed", kind="debate", error=str(exc))

        adr = DebateService(vault_path, dry_run=dry_run).run(
            topic=topic, positions=positions, decision=decision, confidence=confidence
        )
        notification_service.stream_agent_turn(
            actor=f"{role.display_name or role_name} ({role.title})",
            stage="synthesis",
            summary=decision,
            icon="⚖️",
        )
        state_service.record_interaction(
            actor=role_name,
            action="debate",
            target=None,
            summary=topic,
            metadata={"models": models},
        )
        logger.info("debate.complete", role=role_name, models=models, project=project.path.name)
        return adr

    def interactive(self) -> None:
        project = questionary.select(
            "Select project", choices=list(self.projects.projects.keys())
        ).ask()
        if not project:
            return
        task = questionary.select("Select task", choices=list(self.tasks.tasks.keys())).ask()
        if not task:
            return
        extra = questionary.text("Extra instructions (optional)", default="").ask()
        auto_git = questionary.confirm("Run auto-git?", default=False).ask()
        self.run_task(
            project_names=[project], task_name=task, extra_prompt=extra or None, auto_git=auto_git
        )

    def _execute_task(
        self,
        *,
        project: ProjectConfig,
        task_name: str,
        task: TaskConfig,
        extra_prompt: str | None,
        auto_git: bool,
        run_id: int | None = None,
        policy: policy_service.Policy | None = None,
        simulate: bool = False,
        dry_run: bool = True,
        prior_context: str | None = None,
        resume_from_step: int = 0,
        resume_outputs: list[str] | None = None,
        approved_step_index: int | None = None,
        mode: str = "cli",
        stage_skills: list[str] | None = None,
        stage_model: str | None = None,
        stage_effort: EffortLevel | None = None,
        stage: "PipelineStage | None" = None,
        pipeline: "PipelineConfig | None" = None,
    ) -> str | None:
        """Thin OpenTelemetry-tracing wrapper around `_execute_task_body`.

        Opens a `task.run` span (child of the `pipeline.run` span when called
        from `run_pipeline`/`run_task`, a root span otherwise) for the WHOLE
        task execution — including the non-native-engine and role-debate
        early-return paths in `_execute_task_body`. `StepApprovalPending`
        (a pause, not a failure) and `QuotaDeferredError` (a deferral, not a
        failure) are both propagated without being recorded as an error; any
        other exception is recorded on the span + re-raised unchanged. When
        tracing is off/unavailable, `get_tracer()` returns a no-op tracer, so
        this wrapper adds negligible overhead and never changes control
        flow, return values, or propagated exceptions.
        """
        from hivepilot.services.quota import QuotaDeferredError

        _tracer = get_tracer()
        with _tracer.start_as_current_span(
            "task.run",
            attributes={
                "hivepilot.task.name": task_name,
                "hivepilot.task.project": project.path.name,
            },
            # We record exceptions ourselves via `record_exception_on_span`
            # (which redacts secrets first) — disable OTel's own automatic
            # on-exit recording, which would otherwise record the RAW
            # exception a second time when it propagates out of this block.
            record_exception=False,
            set_status_on_exception=False,
        ) as _task_span:
            try:
                result = self._execute_task_body(
                    project=project,
                    task_name=task_name,
                    task=task,
                    extra_prompt=extra_prompt,
                    auto_git=auto_git,
                    run_id=run_id,
                    policy=policy,
                    simulate=simulate,
                    dry_run=dry_run,
                    prior_context=prior_context,
                    resume_from_step=resume_from_step,
                    resume_outputs=resume_outputs,
                    approved_step_index=approved_step_index,
                    mode=mode,
                    stage_skills=stage_skills,
                    stage_model=stage_model,
                    stage_effort=stage_effort,
                    stage=stage,
                    pipeline=pipeline,
                )
            except StepApprovalPending:
                _task_span.set_attribute("hivepilot.task.status", "paused")
                raise
            except RunCancelled:
                _task_span.set_attribute("hivepilot.task.status", "cancelled")
                raise
            except QuotaDeferredError:
                _task_span.set_attribute("hivepilot.task.status", "deferred")
                raise
            except Exception as exc:
                record_exception_on_span(_task_span, exc)
                _task_span.set_attribute("hivepilot.task.status", "failed")
                raise
            _task_span.set_attribute("hivepilot.task.status", "success")
            return result

    def _execute_task_body(
        self,
        *,
        project: ProjectConfig,
        task_name: str,
        task: TaskConfig,
        extra_prompt: str | None,
        auto_git: bool,
        run_id: int | None = None,
        policy: policy_service.Policy | None = None,
        simulate: bool = False,
        dry_run: bool = True,
        prior_context: str | None = None,
        resume_from_step: int = 0,
        resume_outputs: list[str] | None = None,
        approved_step_index: int | None = None,
        mode: str = "cli",
        stage_skills: list[str] | None = None,
        stage_model: str | None = None,
        stage_effort: EffortLevel | None = None,
        stage: "PipelineStage | None" = None,
        pipeline: "PipelineConfig | None" = None,
    ) -> str | None:
        """Execute *task*'s steps for *project*.

        ``resume_from_step``/``resume_outputs``/``approved_step_index``
        (Phase 17a-B) support resuming a task that was previously paused by
        the step-level destructive-operation approval gate
        (``step_requires_approval``) — mirrors how ``run_pipeline``'s
        ``start_index`` resumes a pipeline paused at a stage checkpoint, one
        level finer (step, not stage). ``resume_from_step`` skips already-run
        prior steps (never re-executed — no double side-effects);
        ``resume_outputs`` restores their accumulated output into this call's
        ``outputs`` list; ``approved_step_index`` marks the ONE step index
        that was already approved (so its own gate isn't re-triggered) — any
        LATER destructive step in the same task still gates normally,
        re-pausing the run (see ``StepApprovalPending``).

        ``stage_model``/``stage_effort`` (roles-model-effort-config-owned
        PRD, Sprint 1) are the pipeline/stage-resolved model + reasoning-
        effort defaults for THIS task's role-driven steps (see
        ``resolve_stage_model``/``resolve_effort`` in ``run_pipeline``) —
        threaded into ``hivepilot.roles.resolve_stage_dispatch`` alongside
        ``policy`` at each role-based dispatch site below. Both default to
        ``None`` for a plain (non-pipeline) task run, which resolves
        byte-identically to before these fields existed.

        ``stage``/``pipeline`` (debate-judge-pipeline-yaml PRD, Sprint 2) are
        the raw enclosing ``PipelineStage``/``PipelineConfig`` objects,
        resolved into an ``EffectiveDebateConfig`` via ``_effective_debate``
        below — the single source the dual-model-debate judge call
        (``run_debate``) and the fail-closed verdict gate
        (``perform_git_actions``) both read instead of the global
        ``settings.enable_debate_judge``/``enable_challenge_arbiter``/
        ``judge_runner``/``judge_model``/``judge_confidence_threshold``
        directly. Both default ``None`` for a plain (non-pipeline) task run,
        resolving to the settings floor only — byte-identical to pre-
        Sprint-2 behaviour.
        """
        logger.info("task.start", project=project.path.name, task=task_name)
        metadata: dict[str, Any] = {
            "extra_prompt": extra_prompt or "",
            "prior_context": prior_context or "",
        }
        # Per-pipeline-lessons-yaml PRD, Sprint 2: resolve ONCE per task
        # execution, reuse for the metadata gate below AND every
        # `RunnerPayload` this call constructs -- mirrors `effective_debate`
        # (`self._effective_debate(stage, pipeline)`) just below. `None`
        # `pipeline`/`stage` (a plain, non-pipeline task run) resolves to
        # the settings floor only, byte-identical to pre-Sprint-2 behaviour.
        effective_lessons = resolve_lessons_config(pipeline=pipeline)
        if effective_lessons.enable_distillation:
            # Auto-Learning Lessons Loop PRD, Sprint 3: the ONLY channel a
            # runner (ClaudeRunner/PromptCliRunner) can key its 'Lessons
            # learned' retrieval on -- RunnerPayload carries project_name/
            # task_name natively but has no dedicated role field, and this
            # SAME metadata dict is reused for every step of this task (see
            # the step loop below). None for a non-role task -- retrieval
            # degrades to project+task keying only, never crashes. Gated on
            # the RESOLVED per-pipeline flag (post-review fix, LOW):
            # injection is the only consumer of this key, and a flag-off
            # run must stay byte-identical for every metadata CONSUMER, not
            # just the rendered prompt string -- e.g. the `after_step` hook
            # fan-out passes this SAME dict to every plugin, so an
            # unconditional extra key would leak into flag-off runs too.
            metadata["role"] = task.role
        effective_debate = self._effective_debate(stage, pipeline)
        if task.engine != "native":
            from hivepilot.engines import run_engine

            placeholder_step = (
                task.steps[0]
                if task.steps
                else TaskStep(name=f"{task.engine}-engine", runner="internal")
            )
            payload = RunnerPayload(
                project_name=project.path.name,
                project=project,
                task_name=task_name,
                step=placeholder_step,
                metadata=metadata,
                secrets=self._resolve_secrets(placeholder_step, project, policy),
                lessons=effective_lessons,
            )
            try:
                if simulate:
                    logger.info(
                        "task.simulate.engine", project=project.path.name, engine=task.engine
                    )
                else:
                    run_engine(task=task, project=project, payload=payload)
                if run_id:
                    state_service.record_step(run_id, placeholder_step.name, "success")
            except Exception as exc:
                if run_id:
                    state_service.record_step(run_id, placeholder_step.name, "failed", str(exc))
                raise
            logger.info("task.end", project=project.path.name, task=task_name)
            return None
        if task.role:
            from hivepilot.roles import get_role as _get_role

            _role = _get_role(task.role)
            if _role.models and len(_role.models) > 1:
                topic = extra_prompt or task.description or task_name
                adr = self.run_debate(
                    project_name=project.path.name,
                    role_name=task.role,
                    topic=topic,
                    dry_run=dry_run,
                    simulate=simulate,
                    prior_context=prior_context,
                    debate_config=effective_debate,
                    run_id=run_id,
                    task_name=task_name,
                )
                if run_id:
                    state_service.record_step(run_id, f"{task.role}-debate", "success")
                logger.info("task.end", project=project.path.name, task=task_name)
                adr_path = adr.get("path") if adr else None
                return "dual-model debate → synthesis" + (f" ({adr_path})" if adr_path else "")
        # Stage cache (L3) — skip runner on cache hit
        _stage_cache = None
        _stage_cache_key_val: str | None = None
        if settings.stage_cache_enabled and not simulate and not auto_git:
            from hivepilot.services.stage_cache import get_stage_cache, stage_cache_key

            _stage_cache = get_stage_cache(settings)
            _stage_cache_key_val = stage_cache_key(
                task_name=task_name,
                model=None,  # model resolved per-runner; use None for key stability
                extra_prompt=extra_prompt,
                prior_context=prior_context,
                repo_head=self._get_repo_head(project.path),
            )
            _cached_result = _stage_cache.get(_stage_cache_key_val)
            if _cached_result is not None:
                logger.info("stage.cache_hit", task=task_name, project=project.path.name)
                return _cached_result or None

        # Determine if we should isolate this run in a git worktree
        _use_worktree = (
            settings.worktree_isolation
            and not simulate
            and auto_git
            and (task.git.commit or task.git.push)
            and self._is_git_repo(project.path)
        )

        # Fail-closed guard (Phase 17a-B follow-up): a step-level approval
        # gate is incompatible with git-worktree isolation. `StepApprovalPending`
        # unwinds through `isolated_worktree`'s `with` block, whose `finally`
        # unconditionally `git worktree remove --force`s the worktree — so a
        # mid-task pause would silently discard every prior step's file edits,
        # and resume would then run against a fresh worktree from unchanged
        # HEAD. Checked only on the ORIGINAL run (`approved_step_index is
        # None` — a step-checkpoint resume has this set to the just-approved
        # step, see `run_approved`), so a resume that's already past the gate
        # is never refused; the refusal on the original run is what prevents
        # the unsafe combination from ever starting.
        if _use_worktree and approved_step_index is None:
            _gating_step = _find_gating_step(task, policy, self.registry)
            if _gating_step is not None:
                raise RuntimeError(
                    "Step-level approval (destructive op / require_approval) is "
                    "not supported in a task that uses git worktree isolation "
                    "(auto_git + git.commit/push), because a mid-task pause "
                    "would discard the worktree. Move the destructive step "
                    "into its own task or a pipeline stage with pause_before."
                )

        _wt_ctx = isolated_worktree(project.path) if _use_worktree else nullcontext(project.path)

        with _wt_ctx as _exec_path:
            # Build a shallow copy of the project with the worktree path so both
            # the runner CWD and git actions operate there (branches/commits live
            # in the shared .git; the real working tree is never touched).
            _exec_project = project.model_copy(update={"path": _exec_path})

            outputs: list[str] = list(resume_outputs or [])
            for step_idx, step in enumerate(task.steps):
                if step_idx < resume_from_step:
                    # Already executed (and approved, where relevant) before
                    # the pause that led to this resumed call — never re-run.
                    continue
                # Cooperative cancellation (Mirador actionable dashboard PRD,
                # Sprint 4 — POST /v1/runs/{run_id}/cancel): checked at the
                # top of each step boundary that's actually about to run, as
                # cheaply as possible, BEFORE per-step secrets resolution —
                # a step that's already executing always finishes first (no
                # half-run step is ever left behind). `run_id` is only ever
                # set on the async (POST /v1/runs) path; sync `run_task`
                # callers pass no run_id here, so `is_cancel_requested`
                # always resolves False for them (no registry entry to check
                # against) — cancellation simply never triggers there.
                if run_id is not None and async_run_service.is_cancel_requested(run_id):
                    logger.info(
                        "task.cancelled",
                        project=project.path.name,
                        task=task_name,
                        step=step.name,
                    )
                    state_service.complete_run(
                        run_id, RunStatus.CANCELLED.value, detail="cancelled by operator"
                    )
                    raise RunCancelled(
                        f"Run {run_id} cancelled by operator before step '{step.name}'."
                    )
                secrets = self._resolve_secrets(step, _exec_project, policy)
                payload = RunnerPayload(
                    project_name=_exec_project.path.name,
                    project=_exec_project,
                    task_name=task_name,
                    step=step,
                    metadata=metadata,
                    secrets=secrets,
                    lessons=effective_lessons,
                )
                # Phase 24b.1: tracks the RunnerDefinition actually used/attempted
                # for this step, so record_step(...) below can thread the
                # genuinely-known provider/model — set as soon as runner_def is
                # resolved, and updated to the fallback runner (if any) inside
                # the quota-fallback loop below.
                _used_runner_def: RunnerDefinition | None = None
                from hivepilot.services.quota import QuotaDeferredError

                with get_tracer().start_as_current_span(
                    "step.run",
                    attributes={"hivepilot.step.name": step.name},
                    # We record exceptions ourselves via
                    # `record_exception_on_span` (redacts secrets first) —
                    # disable OTel's own automatic on-exit recording, which
                    # would otherwise record the RAW exception a second time
                    # when it propagates out of this block.
                    record_exception=False,
                    set_status_on_exception=False,
                ) as _step_span:
                    try:
                        if task.role:
                            from typing import cast

                            from hivepilot.models import RunnerDefinition, RunnerKind
                            from hivepilot.roles import (
                                get_role,
                                resolve_host,
                                resolve_stage_dispatch,
                            )

                            runner_kind, role_model, resolved_effort = resolve_stage_dispatch(
                                task.role,
                                policy,
                                stage_model=stage_model,
                                stage_effort=stage_effort,
                            )
                            role_options: dict[str, str] = {}
                            role_perm = get_role(task.role).permission_mode
                            if role_perm:
                                role_options["permission_mode"] = role_perm
                            runner_def = RunnerDefinition(
                                name=f"role:{task.role}",
                                kind=cast(RunnerKind, runner_kind),
                                command=None,
                                model=role_model,
                                effort=resolved_effort,
                                host=resolve_host(task.role, policy),
                                options=role_options,
                            )
                            runner_key = task.role
                        else:
                            runner_key = step.runner_ref or step.runner
                            runner_def = self.registry._definition_for(runner_key)
                        _used_runner_def = runner_def
                        _step_span.set_attribute("hivepilot.step.runner_kind", str(runner_def.kind))
                        # Skill attachment (Sprint 4, skill-plugin-type PRD): resolve this
                        # step's declared skill names -- its own `TaskStep.skills` plus the
                        # enclosing pipeline stage's `PipelineStage.skills` (threaded in via
                        # `stage_skills` when this task run originated from a pipeline stage,
                        # see `run_pipeline` / `resolve_mode`'s sibling threading of `mode`) --
                        # to registered `SkillSpec`s. Materialisation against a concrete
                        # runner (via `apply_skill_if_supported`) happens below, in
                        # `_prepare_payload_for` -- a step/stage that declares no skills
                        # never populates `_resolved_skills`, which keeps that step of
                        # `_prepare_payload_for` a no-op -- byte-identical to before this
                        # wiring (see `hivepilot.runners.base.apply_skill_if_supported`).
                        _skill_names: list[str] = list(step.skills or [])
                        for _stage_skill_name in stage_skills or []:
                            if _stage_skill_name not in _skill_names:
                                _skill_names.append(_stage_skill_name)
                        # Hoisted so it is still in scope at the developer-role
                        # quota-fallback loop further down -- a quota error can
                        # substitute a DIFFERENT-kind runner there, which must have
                        # skills re-applied to it via `_prepare_payload_for`. Empty
                        # when no skills are declared, which keeps that path a
                        # no-op -- byte-identical to before this fix.
                        _resolved_skills: list[SkillSpec] = []
                        if _skill_names:
                            for _skill_name in _skill_names:
                                _skill_spec = self.plugins.get_skill(_skill_name)
                                if _skill_spec is None:
                                    # Should not happen -- config validation (config_validation.py)
                                    # rejects an unregistered skill ref at load time -- but a
                                    # runtime plugin-manager mismatch must degrade, not crash
                                    # a whole run.
                                    logger.warning(
                                        "step.skill_not_found",
                                        step=step.name,
                                        skill=_skill_name,
                                    )
                                    continue
                                _resolved_skills.append(_skill_spec)

                        # Per-attempt payload preparation (mode resolution/
                        # validation/injection + skill re-materialisation),
                        # applied fresh to EACH runner actually attempted -- the
                        # original `runner_def` below and, on a quota error, every
                        # developer-role fallback substituted in the loop further
                        # down. `_base_payload` is the clean, unmaterialised
                        # snapshot of `payload` taken BEFORE any per-kind
                        # enrichment; `_prepare_payload_for` always starts from
                        # IT -- never from a previously-prepared `payload` -- so:
                        #   * a fallback of a DIFFERENT kind is mode-validated
                        #     against ITS OWN `supported_modes`, fail-closed, on
                        #     every attempt (not just the first) -- a mode:api
                        #     step never silently reaches a cli-only fallback;
                        #   * a fallback's skill materialisation never inherits
                        #     the original runner's kind-specific artifacts
                        #     (e.g. a claude scratch dir / appended system
                        #     prompt);
                        #   * `_base_payload` itself is never mutated -- each
                        #     attempt gets its own independently-derived copy
                        #     (`dataclasses.replace`), so concurrent/retried
                        #     attempts can never see each other's mutations.
                        _base_payload = payload

                        def _prepare_payload_for(_rd: RunnerDefinition) -> RunnerPayload:
                            """Return a fresh, per-attempt payload for runner
                            definition *_rd*: effective mode resolved and
                            validated against `_rd`'s `supported_modes`
                            (fail-closed -- raises `RunnerModeUnsupportedError`
                            for an unsupported combination) and injected into a
                            fresh step copy when non-default, then any resolved
                            skills re-applied via `apply_skill_if_supported`.
                            Always derived from `_base_payload`, never from a
                            prior attempt's prepared payload.
                            """
                            from dataclasses import replace as _dc_replace

                            from hivepilot.registry import resolve_runner_class

                            _prepared = _base_payload
                            # Resolve the effective execution mode for this step
                            # and validate it against the runner's declared
                            # capabilities BEFORE any dispatch, so a mode:api
                            # step on a cli-only runner fails closed here -- no
                            # subprocess, no HTTP call. Only inject when a
                            # non-cli mode is actually in effect, keeping the
                            # default (cli) path byte-identical: the runner
                            # would compute the same value from
                            # step.metadata/options anyway, and we never write a
                            # redundant "cli".
                            _mode = _resolve_effective_mode(_base_payload.step, mode)
                            if _mode != "cli":
                                _mode_runner_cls = resolve_runner_class(_rd.kind)
                                validate_runner_mode(
                                    str(_rd.kind),
                                    getattr(
                                        _mode_runner_cls, "supported_modes", frozenset({"cli"})
                                    ),
                                    _mode,
                                )
                                if _prepared.step.metadata.get("mode") != _mode:
                                    _mode_step = _prepared.step.model_copy(
                                        update={
                                            "metadata": {**_prepared.step.metadata, "mode": _mode}
                                        }
                                    )
                                    _prepared = _dc_replace(_prepared, step=_mode_step)
                            if _resolved_skills:
                                _skill_runner_cls = resolve_runner_class(_rd.kind)
                                _skill_runner = _skill_runner_cls(_rd, settings)
                                _prepared = apply_skill_if_supported(
                                    _skill_runner, _prepared, _resolved_skills
                                )
                            return _prepared

                        payload = _prepare_payload_for(runner_def)
                        if payload.step is not step:
                            # Keep the outer `step` variable in sync with the
                            # mode-injected copy -- mirrors the pre-refactor
                            # behaviour where `step` itself was reassigned, so
                            # the destructive-approval gate check and logging
                            # below (which read `step` directly) see the same
                            # metadata the runner will actually receive.
                            step = payload.step
                        if (
                            runner_def.kind == "container"
                            and policy
                            and not policy.allow_containers
                        ):
                            raise RuntimeError(
                                f"Containers are disabled by policy for project {project.path.name}"
                            )
                        # Step-level destructive-operation approval gate (Phase 17a-B) —
                        # mirrors the stage-level `pause_before` checkpoint one level
                        # finer. `approved_step_index` is the ONE step already approved
                        # by a prior `run_approved` resume (its gate must not re-fire);
                        # every other step is checked fresh. `simulate` always bypasses
                        # (no real destructive op runs under --simulate).
                        #
                        # Deliberately checked BEFORE the `before_step` hook below: this
                        # gate only needs `runner_def`/`step`/`payload` (never anything
                        # `before_step` sets up), so checking it first means a step that
                        # pauses here has NOT fired `before_step` yet — preserving the
                        # documented "fired once before each step that actually runs"
                        # contract instead of firing once now (wastefully, before the
                        # pause) and again on resume.
                        if not simulate and step_idx != approved_step_index:
                            _gate_runner = _resolve_runner_for_destructive_check(runner_def)
                            if step_requires_approval(_gate_runner, step, payload):
                                if run_id:
                                    checkpoint_meta = {
                                        "kind": "step_checkpoint",
                                        "task": task_name,
                                        "project": project.path.name,
                                        "extra_prompt": extra_prompt,
                                        "auto_git": auto_git,
                                        "dry_run": dry_run,
                                        "resume_from_step": step_idx,
                                        "step_name": step.name,
                                        "resume_outputs": list(outputs),
                                        "stage_skills": list(stage_skills)
                                        if stage_skills
                                        else None,
                                    }
                                    state_service.record_approval_request(
                                        run_id, project.path.name, task_name, checkpoint_meta
                                    )
                                    notification_service.send_approval_keyboard(
                                        run_id=run_id,
                                        project=project.path.name,
                                        task=f"{task_name} → {step.name}",
                                        details=(
                                            f"Step '{step.name}' performs a destructive "
                                            "operation and requires approval before it runs."
                                        ),
                                    )
                                    state_service.complete_run(run_id, RunStatus.PAUSED.value)
                                logger.info(
                                    "step.approval_required",
                                    project=project.path.name,
                                    task=task_name,
                                    step=step.name,
                                )
                                raise StepApprovalPending(
                                    f"Step '{step.name}' requires approval before executing."
                                )
                        # Hook-only-copy discipline (auto-learning-lessons-
                        # loop PRD, Sprint 1) — but `secrets={}` ONLY, never
                        # `metadata=redact_value(...)`, here: `before_step`
                        # hooks (mem0 `recall`, obsidian `recall`) build
                        # their query from task/step identity, never
                        # `payload.secrets` — confirmed no shipped
                        # `before_step` hook reads `.secrets` (only runner
                        # `capture`/`run` methods do, against the REAL
                        # `payload` passed to the registry below, not this
                        # hook copy). Deliberately NOT overriding `metadata`
                        # here (unlike `after_step`): `recall`'s entire
                        # contract is mutating `payload.metadata[extra_prompt]`
                        # IN PLACE so the runner's later prompt-build sees the
                        # injected memories/vault context (see
                        # `plugins/mem0.py`'s module docstring, "no copy is
                        # made anywhere in between") — `dataclasses.replace`
                        # without a `metadata=` override keeps the SAME dict
                        # reference (shallow copy), so that live-mutation
                        # contract survives; passing a redacted COPY of
                        # metadata here would silently sever it and recalled
                        # memories would stop reaching the agent's prompt.
                        # `metadata` at this point is pre-step input context
                        # (`extra_prompt`/`prior_context`), not a persistence
                        # sink — nothing reads it externally before the
                        # runner does, so redacting it buys no security value
                        # here in exchange for that regression.
                        self.plugins.run_hook(
                            "before_step",
                            payload=replace(payload, secrets={}),
                            dry_run=dry_run,
                            role=task.role,
                        )
                        if simulate:
                            logger.info(
                                "step.simulate",
                                step=step.name,
                                runner=runner_key,
                                project=_exec_project.path.name,
                            )
                            outputs.append(f"[simulated {runner_key}]")
                        elif task.role:
                            from typing import cast

                            from hivepilot.models import RunnerDefinition, RunnerKind
                            from hivepilot.services.quota import parse_quota_error
                            from hivepilot.services.runner_throttle import semaphore_for_kind

                            _runner_def_to_try = runner_def
                            _fallback_runners = (
                                list(settings.dev_fallback_runners)
                                if task.role == "developer"
                                else []
                            )
                            _last_exc: BaseException | None = None

                            while True:
                                _used_runner_def = _runner_def_to_try
                                _sem = semaphore_for_kind(_runner_def_to_try.kind)
                                _sem.acquire()
                                try:
                                    outputs.append(
                                        self.registry.capture_definition(
                                            _runner_def_to_try, payload
                                        )
                                    )
                                    _last_exc = None
                                    break  # success
                                except Exception as _exc:
                                    _last_exc = _exc
                                    _quota_err = parse_quota_error(str(_exc))
                                    if _quota_err is None:
                                        raise  # non-quota error → surface immediately
                                    # Quota error — try fallback runners
                                    if not _fallback_runners:
                                        if task.role == "developer":
                                            from hivepilot.services.quota import QuotaDeferredError

                                            raise QuotaDeferredError(
                                                str(_quota_err.raw), reset_at=_quota_err.reset_at
                                            ) from _exc
                                        raise  # non-developer roles: surface the original exception
                                    _next_kind = _fallback_runners.pop(0)
                                    logger.info(
                                        "dev.fallback",
                                        from_runner=_runner_def_to_try.kind,
                                        to_runner=_next_kind,
                                        reset_at=str(_quota_err.reset_at),
                                    )
                                    if _METRICS_AVAILABLE and _metrics is not None:
                                        try:
                                            _metrics.quota_fallbacks_total.labels(
                                                to_runner=_next_kind
                                            ).inc()
                                        except Exception:  # noqa: BLE001
                                            pass
                                    _runner_def_to_try = RunnerDefinition(
                                        name=f"role:{task.role}:{_next_kind}",
                                        kind=cast(RunnerKind, _next_kind),
                                        command=None,
                                        model=role_model,
                                        effort=resolved_effort,
                                        host=resolve_host(task.role, policy),
                                        options=role_options,
                                    )
                                    # Re-prepare the payload (mode
                                    # validation/injection + skill
                                    # re-materialisation) for the substituted
                                    # runner -- a DIFFERENT kind from the one
                                    # `payload` was prepared for above. Always
                                    # derived fresh from `_base_payload` (see
                                    # `_prepare_payload_for`), never from the
                                    # ORIGINAL runner's already-prepared
                                    # `payload`, so:
                                    #   * a mode:api step whose fallback kind
                                    #     is cli-only fails closed HERE
                                    #     (`RunnerModeUnsupportedError`)
                                    #     instead of silently dispatching in
                                    #     cli mode -- this is NOT a quota
                                    #     error, so it propagates immediately
                                    #     rather than triggering another
                                    #     fallback attempt;
                                    #   * a declared skill is never left inert
                                    #     on the fallback -- it's re-applied
                                    #     against the clean base, never the
                                    #     original runner's kind-specific
                                    #     materialisation (e.g. a claude
                                    #     scratch dir / appended system
                                    #     prompt).
                                    payload = _prepare_payload_for(_runner_def_to_try)
                                finally:
                                    _sem.release()

                            if _last_exc is not None:
                                raise _last_exc
                        else:
                            outputs.append(self._capture_or_execute(runner_key, payload))
                        # Phase 24b.2a: read-and-clear whatever usage the runner's
                        # capture() stashed for THIS step (None when the flag is
                        # off, the runner isn't claude, or nothing was captured).
                        _usage = pop_last_usage()
                        if run_id:
                            _provider, _model = (
                                _resolve_step_provider_model(_used_runner_def, step)
                                if _used_runner_def is not None
                                else (None, None)
                            )
                            _record_step_success(run_id, step.name, _provider, _model, _usage)
                        _step_span.set_attribute("hivepilot.step.status", "success")
                        # Close the store()-redaction hole (auto-learning-
                        # lessons-loop PRD, Sprint 1): `after_step` fans out
                        # to persistence hooks (mem0 `store`, obsidian
                        # `store`, future lesson-distillation sinks) that
                        # were never in the resolved-secrets masking path —
                        # `output` is the step's real captured result,
                        # `payload.metadata`'s `extra_prompt`/`prior_context`
                        # can both echo a resolved `${secret:NAME}` value
                        # verbatim, and `payload.secrets` is the RAW
                        # `{ENV_NAME: resolved_value}` map itself (the exact
                        # values `register_secret_value` masks everywhere
                        # else) — never redactable as text, so it is
                        # unconditionally blanked rather than masked (opus
                        # adversarial review: this is the leak class the
                        # PRD's own next deliverable, lesson-distillation,
                        # will read). Build a COPY, never mutate the shared
                        # `payload`/`metadata` objects: `metadata` is the
                        # SAME dict reused across every step in this task's
                        # loop (see `payload = RunnerPayload(...,
                        # metadata=metadata, ...)` above) and mutating it in
                        # place would permanently corrupt the live prompt
                        # content seen by LATER steps' `before_step`/runner
                        # calls — not just this hook's view. `redact_value`
                        # (`config_provenance.py`) recursively masks strings
                        # nested inside dict/list/tuple and returns a fresh
                        # container, so this also covers non-flat metadata
                        # shapes the earlier top-level-strings-only pass
                        # would have missed.
                        _hook_output = outputs[-1] if outputs else None
                        _redacted_output = (
                            redact_text(_hook_output) if _hook_output else _hook_output
                        )
                        _hook_payload = replace(
                            payload, metadata=redact_value(payload.metadata), secrets={}
                        )
                        self.plugins.run_hook(
                            "after_step",
                            payload=_hook_payload,
                            dry_run=dry_run,
                            role=task.role,
                            output=_redacted_output,
                            # Auto-Learning Lessons Loop PRD, Sprint 4: `run_id`
                            # was already a local in this scope (see `if run_id:`
                            # a few lines above) but was never threaded into this
                            # call -- closes the "run_id omitted" TODO in
                            # `plugins/mem0.py`'s `_provenance_metadata` docstring.
                            # `run_hook` fans out via `**kwargs`, so every other
                            # `after_step` hook (obsidian `store`, etc.) simply
                            # ignores this new kwarg -- no signature change
                            # required anywhere else.
                            run_id=run_id,
                        )
                    except StepApprovalPending:
                        # Not a step failure — the run is already recorded as
                        # PAUSED with an approval request. Propagate unmodified
                        # so `record_step(..., "failed", ...)` below is never hit
                        # and `step.allow_failure` never swallows it.
                        _step_span.set_attribute("hivepilot.step.status", "paused")
                        raise
                    except QuotaDeferredError:
                        # Not a step failure either — mirrors `StepApprovalPending`
                        # above: a quota deferral is an interrupt that the
                        # pipeline-level handler (`_run_task_body`'s
                        # `except QuotaDeferredError`, reached via `run_task`)
                        # turns into a retry-later `RunResult` +
                        # `complete_run(..., "deferred")`.
                        # Propagate unmodified so `record_step(..., "failed", ...)`
                        # below is never hit and `step.allow_failure` never
                        # swallows it as a recovered failure.
                        _step_span.set_attribute("hivepilot.step.status", "deferred")
                        raise
                    except Exception as exc:
                        # Defensive clear: an exception means capture() raised
                        # before reaching its usage-stash point, so this is
                        # expected to be None — but pop unconditionally so no
                        # stale usage from a partial attempt leaks into the next
                        # step's success path.
                        record_exception_on_span(_step_span, exc)
                        _step_span.set_attribute("hivepilot.step.status", "failed")
                        pop_last_usage()
                        if run_id:
                            _provider, _model = (
                                _resolve_step_provider_model(_used_runner_def, step)
                                if _used_runner_def is not None
                                else (None, None)
                            )
                            state_service.record_step(
                                run_id,
                                step.name,
                                "failed",
                                str(exc),
                                provider=_provider,
                                model=_model,
                            )
                        if step.allow_failure:
                            logger.warning("step.failure_allowed", step=step.name, error=str(exc))
                            continue
                        raise

            task_result = "\n".join(o for o in outputs if o).strip() or None

            # Stage cache (L3) — store result on success
            if _stage_cache is not None and _stage_cache_key_val is not None and task_result:
                _stage_cache.put(_stage_cache_key_val, task_result)

            if auto_git:
                perform_git_actions(
                    project_name=project.path.name,  # real component name → correct branch name
                    project=_exec_project,  # worktree path → git ops run in the worktree
                    git=task.git,
                    # gate promote_pr/merge_pr on THIS stage's own verdict — see
                    # git_service._agent_verdict_blocked for why this is required.
                    task_result=task_result,
                    # Sprint 3: additionally fail-closed-gate on the most-blocking
                    # debate-judge/challenge-arbiter Verdict registered so far
                    # THIS run (see `_register_verdict`) — a None/empty/
                    # low-confidence/non-approval verdict blocks promote/merge
                    # exactly like an explicit blocking `status:` does. Only
                    # active when either judge feature is opt-in enabled;
                    # flags-off is byte-identical to pre-Sprint-3 behaviour.
                    verdict=self._governing_verdict,
                    judge_gate_enabled=(
                        effective_debate.enable_judge or effective_debate.enable_arbiter
                    ),
                    confidence_threshold=effective_debate.confidence_threshold,
                )

        logger.info("task.end", project=project.path.name, task=task_name)
        return task_result

    def _capture_or_execute(self, runner_key: str, payload: RunnerPayload) -> str:
        """Run a non-role step, capturing its stdout when the runner supports it
        (so the agent's output surfaces in the interaction log / stream)."""
        runner = self.registry.get_runner(runner_key)
        capture = getattr(runner, "capture", None)
        if capture is not None:
            return capture(payload)
        runner.run(payload)
        return ""

    @staticmethod
    def _get_repo_head(path: Path) -> str | None:
        """Return the current git HEAD sha for a repo path, or None on error."""
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        """Return True if *path* is inside a git repository."""
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _project(self, name: str) -> ProjectConfig:
        if name not in self.projects.projects:
            raise ValueError(f"Unknown project: {name}")
        return self.projects.projects[name]

    def remediation_gate_present(self, project_name: str, task_name: str) -> bool:
        """Preflight, side-effect-free check: would running *task_name*
        against *project_name* trip the step-level approval gate before any
        step actually executes?

        Used by gated auto-remediation callers (e.g. `drift_schedule.
        _attempt_remediation`, Phase 20 D4) to refuse dispatching a
        `remediate_task` that isn't actually destructive/gated --
        `step_requires_approval` is fail-OPEN for a step whose runner has no
        `is_destructive` method (or whose resolved operation isn't
        apply/destroy), so a misconfigured `remediate_task` would otherwise
        run un-approved via `run_task`.

        Resolves the task and policy exactly the way `run_task`/
        `_run_task_body` does (`self.tasks.tasks[task_name]`,
        `policy_service.enforce_policy` — always with `auto_git=False` here,
        since gated remediation never uses auto_git), then delegates to the
        same static, non-executing `_find_gating_step` the worktree-isolation
        refusal (`_execute_task`) already relies on to agree with the real
        gate. Fail-closed: an unknown task/project, or any resolution error,
        returns False (not gated) -- never True by default.
        """
        try:
            if task_name not in self.tasks.tasks:
                return False
            task = self.tasks.tasks[task_name]
            project = self._project(project_name)
            policy = policy_service.enforce_policy(project.path.name, auto_git=False)
        except Exception:  # noqa: BLE001 -- fail-closed: unresolvable -> not gated
            return False
        return _find_gating_step(task, policy, self.registry) is not None

    def _cve_gate_block_detail(
        self, project: ProjectConfig, tool: str, severity: str
    ) -> str | None:
        """Phase 21 Sprint 2 — evaluate the pipeline CVE gate for *project*.

        Reuses `scan_service.scan_vulnerabilities`/`exceeds_severity`
        directly (no re-implementation of scanning here). Returns a
        block-reason string when the run must be blocked, or `None` when it
        may proceed.

        Fail-closed: ANY exception from `scan_vulnerabilities` — missing
        scanner binary, timeout, unexpected exit code, unparseable output —
        is treated as a block, never as "proceed". A CVE gate the operator
        opted into (`policy.block_on_severity`) must never silently pass when
        it cannot actually run the scan.

        Anti-leak: the returned detail is persisted (`state_service.
        complete_run`) and sent to notifications, so it must never contain
        raw scanner stdout or a specific package/CVE identifier that could
        embed lockfile/source material — only the `by_severity` COUNTS dict
        `scan_service` already guarantees is leak-free (see scan_service's
        module docstring) plus the exception's type name (never its message,
        which could echo a path or scanner-reported detail).
        """
        try:
            scan_result = scan_service.scan_vulnerabilities(
                project.path, tool=tool, severity_threshold=severity
            )
            if scan_service.exceeds_severity(scan_result, severity):
                return (
                    f"Blocked by CVE gate: {scan_result.by_severity} — findings at/above {severity}"
                )
        except Exception as exc:  # noqa: BLE001 — fail-closed, see docstring above.
            logger.error(
                "run.cve_gate_scan_failed",
                project=project.path.name,
                tool=tool,
                severity=severity,
                error_type=type(exc).__name__,
            )
            return f"CVE gate configured but scan failed: {type(exc).__name__}"

        return None

    def _resolve_secrets(
        self,
        step: TaskStep,
        project: ProjectConfig | None = None,
        policy: policy_service.Policy | None = None,
    ) -> dict[str, str]:
        """Resolve this step's secrets into a ``name -> value`` mapping for the
        runner environment.

        Two forms are combined:
          * the direct ``step.secrets`` form (``{ENV_NAME: {source, key}}``),
            resolved through the existing ``secret_resolver``;
          * ``${secret:NAME}`` references embedded in ``project.env`` values,
            resolved LAZILY here against the project ``secrets:`` catalog. The
            resolved value overrides the raw ``project.env`` entry (payload
            secrets win in ``merge_environments``).

        Every resolved value is registered for masking so it can never appear
        verbatim in logs / provenance / serialized state.
        """
        resolved: dict[str, str] = {}
        if step.secrets:
            resolved.update(secret_resolver.resolve(step.secrets))

        # Scan project.env for ${secret:NAME} references whenever env is present
        # (not gated on a non-empty catalog: a reference against an EMPTY catalog
        # is an unresolved reference and must abort in closed mode).
        if project is not None and project.env:
            fail_mode = policy.secrets_fail_mode if policy is not None else "closed"
            resolved.update(
                resolve_secret_refs(
                    project.env,
                    catalog=project.secrets,
                    fail_mode=fail_mode,
                )
            )

        # Register direct-form values too: the reference path registers inside
        # resolve_secret_refs, but direct-form secrets must be masked as well.
        for value in resolved.values():
            register_secret_value(value)
        return resolved

    def _collect_artifacts(
        self,
        *,
        manager: ArtifactManager,
        task: TaskConfig,
        projects: list[ProjectConfig],
    ) -> None:
        capture = task.artifacts.get("capture", ["diff"])
        if "diff" in capture:
            for project in projects:
                diff = self._git_diff(project.path)
                if diff:
                    manager.write_file(f"diffs/{project.path.name}.patch", diff)

    @staticmethod
    def _git_diff(path: Path) -> str | None:
        result = subprocess.run(
            ["git", "diff"],
            cwd=str(path),
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout if result.stdout.strip() else None
