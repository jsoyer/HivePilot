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
  same "build ONE prompt, make ONE runner call, parse the JSON" shape).
- Deliberately does NOT import `hivepilot.services.state_service` (avoids a
  circular import: `state_service.record_lesson` takes plain primitives, not
  a `Lesson`, precisely so this module and `state_service` never need to
  import each other).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, cast

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
    the validation score (Sprint 3's job, from real outcome signal).
    """

    text: str
    category: str | None
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
    make ONE runner call, run the raw output through `redact_text` (S1's
    run-scope masking choke point -- any resolved `${secret:NAME}` value an
    agent echoed anywhere in the verdicts/interactions/outcomes text must be
    masked before it can reach a parsed lesson), then parse.

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

    prompt = build_distill_prompt(
        project=project.path.name if project else None,
        role=role,
        task=task,
        verdicts=verdicts,
        interactions=interactions,
        outcomes=outcomes,
    )
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
