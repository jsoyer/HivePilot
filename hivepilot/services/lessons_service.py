"""
Lesson distillation — opt-in, ONE-LLM-call-per-run synthesis of a pipeline
run's verdicts + interactions + outcomes into structured, unscored CANDIDATE
lessons (Auto-Learning Lessons Loop PRD, Sprint 2).

Design invariants:
- The distiller proposes TEXT/category ONLY. Any ``score``/``confidence`` the
  LLM self-reports is NEVER read as the validation score -- Sprint 3 computes
  the real score from actual outcome signal. This module's parser doesn't
  even look for those keys.
- Never fabricates a lesson: a malformed/empty/non-JSON/non-list response, or
  a list item missing a non-empty ``text``, is dropped. A wholly unusable
  response returns ``[]`` -- nothing is ever stored on a doubt.
- ONE `capture_definition` call per run (mirrors
  `Orchestrator._adjudicate`/`_adjudicate_challenge` in `orchestrator.py` --
  same "build ONE prompt, make ONE runner call, parse the JSON" shape). The
  call is skipped entirely when there's no real signal to distill (no
  verdicts AND no interactions) -- an outcome-only run isn't worth a costed
  LLM call.
- Redaction guards BOTH directions, not just persistence: the fully-assembled
  prompt is passed through `redact_text` immediately before the `capture_fn`
  call (egress choke point) -- `outcomes[].detail` in particular is sourced
  from `RunResult.detail`, a field known to reach other sinks in cleartext
  (see `hivepilot/orchestrator.py`'s `RunResult` choke-point comment), so
  without this a resolved `${secret:NAME}` value could leave the trust
  boundary via the prompt even though the (separately redacted) response
  never echoed it back. The response is ALSO redacted after the call
  (belt-and-suspenders, guards the persisted `lessons` row against anything
  the distiller itself might echo).
- Deliberately does NOT import `hivepilot.services.state_service` (avoids a
  circular import: `state_service.record_lesson` takes plain primitives, not
  a `Lesson`, precisely so this module and `state_service` never need to
  import each other).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable, cast

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, RunnerKind, TaskStep
from hivepilot.runners.base import RunnerPayload

# A capture function has the same shape as `RunnerRegistry.capture_definition`
# -- injected by the caller (the orchestrator passes its own live,
# plugin-aware `self.registry.capture_definition`) so this module never has
# to instantiate its own `RunnerRegistry` (and tests can mock it directly
# without standing up a real Orchestrator).
CaptureFn = Callable[[RunnerDefinition, RunnerPayload], str]


@dataclass(frozen=True)
class Lesson:
    """A single distilled lesson CANDIDATE.

    Only ``text``/``category`` come from the distiller's own judgment --
    ``source_verdict_id``/``source_interaction_id`` are optional pointers
    back to the specific `verdicts`/`interactions` row (if any) the
    distiller cited, echoed back so `record_lesson` can persist them as the
    `lessons` table's FK columns. There is deliberately NO `score`/
    `confidence` field here: this module never trusts an LLM self-report as
    the validation score (Sprint 3's job, from real outcome signal). See
    `ValidatedLesson` (Sprint 3) for the DISTINCT type `retrieve_lessons`
    returns -- keeping that a separate dataclass (rather than adding
    `id`/`score` fields here) preserves this invariant as something
    `hasattr(candidate, "score")` can still assert False on, not just "score
    happens to be None".
    """

    text: str
    category: str | None
    source_verdict_id: int | None = None
    source_interaction_id: int | None = None


@dataclass(frozen=True)
class ValidatedLesson:
    """A VALIDATED lesson read back from the `lessons` table (Sprint 3) --
    ready for ranking/injection into a future run's prompt.

    Deliberately a SEPARATE type from `Lesson` (the distillation
    CANDIDATE): `retrieve_lessons` only ever builds these from an
    already-persisted, already-`validated=1` row (`state_service.
    list_ranked_lessons`), so ``score`` here is ALWAYS the real,
    outcome-derived value `validate_lesson` computed and
    `update_lesson_validation` persisted -- never the distiller's own
    self-report (`Lesson` has no such field to leak from in the first
    place).
    """

    id: int
    text: str
    category: str | None
    score: float | None
    source_verdict_id: int | None = None
    source_interaction_id: int | None = None


_DISTILL_PROMPT_TEMPLATE = (
    "You are reviewing the record of one completed automation run to extract "
    "reusable lessons for FUTURE runs of the same kind of task.\n\n"
    "RUN CONTEXT:\nproject={project}\nrole={role}\ntask={task}\n\n"
    "JUDGE/ARBITER VERDICTS FROM THIS RUN:\n{verdicts_block}\n\n"
    "AGENT INTERACTIONS FROM THIS RUN:\n{interactions_block}\n\n"
    "OUTCOMES FROM THIS RUN:\n{outcomes_block}\n\n"
    "Extract zero or more concise, reusable lessons that would help an agent "
    "do better on a SIMILAR future task. Only propose a lesson when the "
    "record actually supports it -- do not invent lessons from nothing, and "
    "prefer returning fewer, high-quality lessons over padding the list.\n\n"
    "Respond with ONLY a single JSON array -- no prose, no markdown code "
    "fences -- where each element matches exactly this shape:\n"
    '{{"text": "<one or two sentence, reusable lesson>", '
    '"category": "<short category tag, e.g. \\"testing\\", \\"security\\", '
    '\\"performance\\">", '
    '"source_verdict_id": <int or null>, '
    '"source_interaction_id": <int or null>}}\n\n'
    "If nothing in the record supports a confident, reusable lesson, respond "
    "with an empty array: []. Never fabricate a lesson you are not confident "
    "about."
)


def _format_verdicts(verdicts: list[dict[str, Any]]) -> str:
    if not verdicts:
        return "(none)"
    lines = []
    for v in verdicts:
        lines.append(
            f"- id={v.get('id')} kind={v.get('kind')} decision={v.get('decision')!r} "
            f"confidence={v.get('confidence')} summary={v.get('summary')!r}"
        )
    return "\n".join(lines)


def _format_interactions(interactions: list[dict[str, Any]]) -> str:
    if not interactions:
        return "(none)"
    lines = []
    for i in interactions:
        lines.append(
            f"- id={i.get('id')} actor={i.get('actor')} action={i.get('action')} "
            f"target={i.get('target')} summary={i.get('summary')!r}"
        )
    return "\n".join(lines)


def _format_outcomes(outcomes: list[dict[str, Any]]) -> str:
    if not outcomes:
        return "(none)"
    lines = []
    for o in outcomes:
        lines.append(
            f"- project={o.get('project')} target={o.get('target')} "
            f"success={o.get('success')} detail={o.get('detail')!r}"
        )
    return "\n".join(lines)


def build_distill_prompt(
    *,
    project: str | None,
    role: str | None,
    task: str | None,
    verdicts: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> str:
    """Render the ONE distillation prompt covering *verdicts*/*interactions*/
    *outcomes* for a single completed run."""
    return _DISTILL_PROMPT_TEMPLATE.format(
        project=project or "(unknown)",
        role=role or "(unknown)",
        task=task or "(unknown)",
        verdicts_block=_format_verdicts(verdicts),
        interactions_block=_format_interactions(interactions),
        outcomes_block=_format_outcomes(outcomes),
    )


def parse_distilled_lessons(raw: str) -> list[Lesson]:
    """Parse the distiller's raw text response into a list of :class:`Lesson`.

    Parse rules (Sprint 2 contract, mirrors `orchestrator._parse_verdict`'s
    discipline):
      * Empty/whitespace-only text -> ``[]``.
      * Tolerates a ```json ... ``` fenced block around the JSON array.
      * Non-JSON, or a JSON value that isn't a list -> ``[]``.
      * Each element must be a dict with a non-empty string ``text`` --
        elements failing this are dropped (not fatal to the rest of the
        list).
      * ``category``, when present and a non-empty string, is kept as-is;
        otherwise defaults to ``"general"``.
      * ``source_verdict_id``/``source_interaction_id``, when present, must
        be an ``int`` (bool excluded) or ``None`` -- any other type drops
        just that pointer (does not invalidate the element).
      * Any self-reported ``score``/``confidence``/other keys are IGNORED --
        this parser never reads them, by design (see module docstring).

    NEVER fabricates a lesson: a malformed/empty/non-JSON/non-list response
    returns ``[]`` -- nothing is stored on a doubt.
    """
    text = raw.strip() if raw else ""
    if not text:
        return []

    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(data, list):
        return []

    lessons: list[Lesson] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        lesson_text = item.get("text")
        if not isinstance(lesson_text, str) or not lesson_text.strip():
            continue

        category_raw = item.get("category")
        category = (
            category_raw.strip()
            if isinstance(category_raw, str) and category_raw.strip()
            else "general"
        )

        source_verdict_id = item.get("source_verdict_id")
        if not isinstance(source_verdict_id, int) or isinstance(source_verdict_id, bool):
            source_verdict_id = None

        source_interaction_id = item.get("source_interaction_id")
        if not isinstance(source_interaction_id, int) or isinstance(source_interaction_id, bool):
            source_interaction_id = None

        lessons.append(
            Lesson(
                text=lesson_text.strip(),
                category=category,
                source_verdict_id=source_verdict_id,
                source_interaction_id=source_interaction_id,
            )
        )
    return lessons


def build_distiller_definition(
    *,
    runner: str,
    model: str | None,
) -> RunnerDefinition:
    """Build the distiller `RunnerDefinition` from
    `settings.lesson_distill_runner`/`lesson_distill_model` (fallback model
    handling mirrors how the debate judge builds its `RunnerDefinition` in
    `Orchestrator._resolve_challenge_via_arbiter`: `model=None` lets the
    runner fall back to its own default)."""
    return RunnerDefinition(
        name="lessons:distiller",
        kind=cast(RunnerKind, runner),
        command=None,
        model=model,
    )


def distill_lessons(
    *,
    run_id: int | None,
    project: ProjectConfig,
    role: str | None = None,
    task: str | None = None,
    verdicts: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    distiller_def: RunnerDefinition,
    capture_fn: CaptureFn,
) -> list[Lesson]:
    """Distill *verdicts*/*interactions*/*outcomes* from ONE completed run
    into structured lesson CANDIDATEs via ONE `capture_fn` (i.e.
    `RunnerRegistry.capture_definition`) call.

    Mirrors `Orchestrator._adjudicate`'s shape exactly: build ONE prompt,
    make ONE runner call, parse the JSON response.

    Skips the call entirely (returns ``[]``) when *verdicts* AND
    *interactions* are both empty -- an outcome-only run has near-zero
    signal to distill from, so a costed LLM call isn't warranted (Sprint 2
    review finding, LOW).

    Redacts the FULLY ASSEMBLED PROMPT via `redact_text` immediately before
    the `capture_fn` call -- the egress choke point. This is deliberately
    NOT the same as redacting individual fields going in: `outcomes[].detail`
    is sourced from `RunResult.detail`, which is NOT pre-redacted upstream
    (unlike `verdicts.summary`/`interactions.summary`, both already redacted
    at `record_verdict`/`record_interaction` INSERT time) -- a resolved
    `${secret:NAME}` value sitting in a step's failure detail would
    otherwise be sent VERBATIM to the external `lesson_distill_runner`
    model, i.e. leave the trust boundary, even though the (separately
    redacted) response could never leak it back into the persisted
    `lessons` row. Redacting the whole prompt string closes that regardless
    of which field the secret came from. The raw response is ALSO run
    through `redact_text` after the call (belt-and-suspenders, S1's
    run-scope masking choke point) before parsing.

    *capture_fn* is injected by the caller (the orchestrator's own live
    `self.registry.capture_definition`, same registry instance used for the
    rest of the run) rather than this module instantiating its own
    `RunnerRegistry` -- keeps this module registry-agnostic and trivially
    mockable in tests.

    Never fabricates a lesson -- see `parse_distilled_lessons`'s contract.
    A `capture_fn` call that itself raises (network/runner error) IS allowed
    to propagate -- the caller (orchestrator wiring) is responsible for
    catching it as best-effort, same as every other post-run side-effect in
    `_run_task_body`.
    """
    from hivepilot.services.config_provenance import redact_text

    if not verdicts and not interactions:
        return []

    prompt = build_distill_prompt(
        project=project.path.name if project else None,
        role=role,
        task=task,
        verdicts=verdicts,
        interactions=interactions,
        outcomes=outcomes,
    )
    # Egress choke point: redact the FULL prompt -- not just the response --
    # before it ever reaches `capture_fn` (i.e. before it's sent to the
    # external distiller model). See the docstring above for why this must
    # be whole-prompt rather than per-field.
    prompt = redact_text(prompt)
    step = TaskStep(name="lessons-distiller", runner=distiller_def.kind, prompt_file=None)
    payload = RunnerPayload(
        project_name=project.path.name,
        project=project,
        task_name=f"lessons:{task or 'run'}:distill",
        step=step,
        metadata={"extra_prompt": prompt, "prior_context": ""},
        secrets={},
    )
    raw = capture_fn(distiller_def, payload)
    raw = redact_text(raw) if raw else raw
    return parse_distilled_lessons(raw or "")


# ---------------------------------------------------------------------------
# Sprint 3 -- fail-closed validation gate + scored retrieval
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeSignal:
    """The REAL, post-hoc outcome signal a distilled lesson CANDIDATE is
    validated against (Auto-Learning Lessons Loop PRD, Sprint 3) --
    NEVER the distiller's own self-report (see `Lesson`'s docstring for why
    that field doesn't even exist on the candidate).

    Every field defaults to its DENY value (``False`` / ``False`` /
    ``None``) -- this is deliberate and is the core anti-poisoning
    property of this module: an *absent* or *empty* ``OutcomeSignal``
    (e.g. a run that produced no verdicts, no resolved challenge, and
    wasn't itself successful) is indistinguishable, on purpose, from "no
    real signal happened" and MUST be treated as DENY by `validate_lesson`
    -- never as "no constraint -> allow". This is the same failure class
    tracked project-wide as an empty-value-fail-open gate (a `[]`/`None`
    sentinel silently inverting a security check); this dataclass exists
    specifically so "empty" has one unambiguous shape instead of leaking
    into ad-hoc `None`/missing-key checks scattered across callers.

    Always built from actual `runs`/`verdicts`/`interactions` rows (see
    `Orchestrator._build_lesson_outcome_signal`) -- never from the
    distiller's `capture_fn` response.
    """

    run_success: bool = False
    resolved_challenge: bool = False
    max_verdict_confidence: float | None = None


def validate_lesson(lesson: Lesson, outcome_signal: OutcomeSignal | None) -> tuple[bool, float]:
    """Validate *lesson* against *outcome_signal* and return
    ``(validated, score)``.

    Score is computed EXCLUSIVELY from *outcome_signal* -- this function
    doesn't even look at anything the distiller self-reported (`Lesson`
    has no `score`/`confidence` field to read in the first place, by
    design). The score is the MAX of whichever real signals are present:
      * ``run_success=True``        -> candidate score ``1.0``
      * ``resolved_challenge=True``  -> candidate score ``1.0``
      * ``max_verdict_confidence``, when a finite value in ``[0, 1]`` ->
        that confidence value itself (the actual judge/arbiter confidence
        for a resolved challenge on this run)

    FAIL-CLOSED (the core security property of this Sprint -- see
    `OutcomeSignal`'s docstring): *outcome_signal* being ``None``, or
    carrying no positive signal at all (every field at its DENY default),
    quarantines the lesson -- returns ``(False, 0.0)``. Absent/empty
    signal is treated as DENY, never as "no constraint -> allow" (the
    empty-value-fail-open bug class this module exists to avoid).

    ``validated`` is ``True`` only when the computed score is
    ``>= settings.lesson_min_score`` -- itself fail-closed-validated to a
    finite value in ``(0.0, 1.0]`` at `Settings` construction (see
    `Settings._validate_lesson_min_score`), so this gate can never be
    silently defeated by a misconfigured floor of ``0``.
    """
    if outcome_signal is None:
        return False, 0.0

    candidate_scores: list[float] = []
    if outcome_signal.run_success:
        candidate_scores.append(1.0)
    if outcome_signal.resolved_challenge:
        candidate_scores.append(1.0)
    confidence = outcome_signal.max_verdict_confidence
    if confidence is not None and math.isfinite(confidence) and 0.0 <= confidence <= 1.0:
        candidate_scores.append(confidence)

    if not candidate_scores:
        # No real signal at all (every field at its DENY default) -- DENY,
        # never "no constraint -> allow". This is the explicit
        # empty-value-fail-open guard.
        return False, 0.0

    score = max(candidate_scores)
    validated = score >= settings.lesson_min_score
    return validated, score


def retrieve_lessons(
    project: str,
    role: str | None = None,
    task: str | None = None,
    *,
    limit: int = 5,
    semantic: bool = False,
) -> list[ValidatedLesson]:
    """Retrieve VALIDATED lessons for *project* (optionally narrowed by
    *role*/*task*), ranked by score (desc) then recency (desc), capped at
    *limit* (Auto-Learning Lessons Loop PRD, Sprint 3).

    ``semantic=False`` (the only path implemented this Sprint) is a plain,
    dependency-free SQLite read via `state_service.list_ranked_lessons` --
    no `mem0`/`FAISS`/`langchain` import anywhere on this path, so the core
    lessons loop keeps working with those optional dependencies absent.
    ``semantic=True`` is a deliberate Sprint 4 seam: it raises
    `NotImplementedError` rather than silently falling back to keyword
    ranking, so a caller that flips it on early gets a loud signal instead
    of a quietly-wrong result.

    Only ever returns rows with ``validated=1`` -- `state_service.
    list_ranked_lessons` hard-codes that filter (no toggle), so an
    unvalidated candidate can never leak into this path regardless of what
    a caller passes.
    """
    if semantic:
        raise NotImplementedError(
            "Semantic lesson retrieval lands in Sprint 4 of the "
            "auto-learning-lessons-loop PRD -- not implemented yet."
        )

    from hivepilot.services import state_service

    rows = state_service.list_ranked_lessons(project, role=role, task=task, limit=limit)
    return [
        ValidatedLesson(
            id=row["id"],
            text=row.get("text") or "",
            category=row.get("category"),
            score=row.get("score"),
            source_verdict_id=row.get("source_verdict_id"),
            source_interaction_id=row.get("source_interaction_id"),
        )
        for row in rows
    ]
