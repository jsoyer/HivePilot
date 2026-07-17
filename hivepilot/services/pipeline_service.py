from __future__ import annotations

from hivepilot.models import PipelineConfig, TasksFile


def validate_pipeline(pipeline: PipelineConfig, tasks: TasksFile) -> None:
    for stage in pipeline.stages:
        if stage.task not in tasks.tasks:
            raise ValueError(
                f"Pipeline stage '{stage.name}' references missing task '{stage.task}'"
            )
    validate_roles(tasks)


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
