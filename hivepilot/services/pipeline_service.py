from __future__ import annotations

import math

from hivepilot.models import PipelineConfig, TasksFile, resolve_debate_config


def validate_pipeline(pipeline: PipelineConfig, tasks: TasksFile) -> None:
    for stage in pipeline.stages:
        if stage.task not in tasks.tasks:
            raise ValueError(
                f"Pipeline stage '{stage.name}' references missing task '{stage.task}'"
            )
    validate_roles(tasks)
    validate_debate_config(pipeline)
    validate_review_config(pipeline)
    validate_lessons_config(pipeline)


def _validate_confidence_threshold(value: float | None, *, where: str) -> None:
    """Defense-in-depth re-check of `DebateConfig.confidence_threshold`.

    `DebateConfig`'s own pydantic field validator already rejects an
    out-of-range value at YAML-load time (see hivepilot/models.py) -- this is
    a second, independent guard at the point `validate_pipeline` is called
    (before any stage executes), so a value that somehow slipped past model
    construction (e.g. via `model_construct`) still fails closed here rather
    than silently reaching the fail-closed PR gate as a bad threshold. Absent
    (`None`) is always valid -- it means "inherit the global floor", never
    "no threshold" / "always pass".
    """
    if value is None:
        return
    if not math.isfinite(value) or not (0 < value <= 1):
        raise ValueError(
            f"{where} debate.confidence_threshold must be a finite number in (0, 1], got {value!r}"
        )


def validate_debate_config(pipeline: PipelineConfig) -> None:
    """Fail closed on an out-of-range `debate.confidence_threshold` at both
    the pipeline level and every stage level. See `_validate_confidence_threshold`
    for why this re-checks what `DebateConfig`'s pydantic validator already
    enforces at construction time."""
    if pipeline.debate is not None:
        _validate_confidence_threshold(pipeline.debate.confidence_threshold, where="Pipeline")
    for stage in pipeline.stages:
        if stage.debate is not None:
            _validate_confidence_threshold(
                stage.debate.confidence_threshold, where=f"Pipeline stage '{stage.name}'"
            )


def validate_review_config(pipeline: PipelineConfig) -> None:
    """Fail closed on `debate.review_target` requiring at least one resolved
    reviewer, re-checked cross-block at pipeline LOAD time (before any stage
    executes).

    `resolve_debate_config`'s own resolve-time backstop (see
    `hivepilot/models.py`) already enforces this fail-closed rule for a
    single stage's resolution -- calling it here, once for the pipeline-only
    view (`stage=None`, covers a pipeline-level `review_target` when the
    pipeline has zero stages or no stage overrides it) and once per stage,
    surfaces the SAME error at config load instead of only being caught
    mid-run when the offending stage actually executes. Mirrors
    `validate_debate_config`'s shape (delegates to the existing fail-closed
    check rather than duplicating its logic).
    """
    resolve_debate_config(pipeline=pipeline, stage=None)
    for stage in pipeline.stages:
        resolve_debate_config(pipeline=pipeline, stage=stage)


def validate_lessons_config(pipeline: PipelineConfig) -> None:
    """Fail closed on an out-of-range `lessons.min_score`/`inject_limit` at
    the pipeline level. Defense-in-depth re-check of `LessonsConfig`'s own
    pydantic field validators (see hivepilot/models.py) -- a value that
    somehow slipped past model construction (e.g. via `model_construct`)
    still fails closed here rather than silently reaching the distillation/
    retrieval gate as an allow-all `min_score` or a disabled `inject_limit`
    floor. Mirrors `validate_debate_config`'s shape (this config has no
    stage-level tier, so there is only the pipeline-level check).
    """
    lessons = pipeline.lessons
    if lessons is None:
        return
    if lessons.min_score is not None and (
        not math.isfinite(lessons.min_score) or not (0 < lessons.min_score <= 1)
    ):
        raise ValueError(
            f"Pipeline lessons.min_score must be a finite number in (0, 1], "
            f"got {lessons.min_score!r}"
        )
    if lessons.inject_limit is not None and lessons.inject_limit < 1:
        raise ValueError(
            f"Pipeline lessons.inject_limit must be >= 1, got {lessons.inject_limit!r}"
        )


def validate_roles(tasks: TasksFile) -> None:
    """Fail closed if any task references a role that isn't loaded.

    Sprint 2 of the roles-model-effort-config-owned PRD reduced the code-owned
    `_DEFAULT_ROLES` fallback to a single generic `developer` role — a
    deployment with no custom `roles.yaml` (or one that dropped a role a task
    still references) would otherwise hit a bare `KeyError` deep inside
    `hivepilot.roles.resolve_runner`/`get_role` at dispatch time, well after
    the run has already started. Checking here, at the same point
    `validate_pipeline` already checks task existence (before any stage
    executes), converts that into an actionable error naming the task, the
    unknown role, and where to define it.
    """
    from hivepilot.roles import ROLES  # local import: always the current, possibly-refreshed dict

    for task_name, task in tasks.tasks.items():
        role_name = task.role
        if role_name and role_name not in ROLES:
            raise ValueError(
                f"Task '{task_name}' references unknown role '{role_name}'. "
                f"Define '{role_name}' in your roles.yaml (see examples/roles.yaml "
                f"for a restorable template of the previous business roles), or "
                f"point the task at an existing role: {sorted(ROLES)}."
            )
