"""Tests for hivepilot.services.pipeline_service.

Covers:
- validate_pipeline still fails closed on a missing task (pre-existing).
- validate_pipeline / validate_roles fail closed on a task that references
  a role not present in ROLES, with an actionable error naming the task,
  the unknown role, and where to define it (roles-model-effort-config-owned
  PRD, Sprint 2).
- A task with role=None (no role bound) never trips the role check.
- The real, unmodified root roles.yaml + tasks.yaml + pipelines.yaml
  (this repo's own dogfooded "company" config) still validates cleanly.
"""

from __future__ import annotations

import pytest

from hivepilot.models import PipelineConfig, PipelineStage, TaskConfig, TasksFile
from hivepilot.services.pipeline_service import validate_pipeline, validate_roles


def _tasks(**tasks: TaskConfig) -> TasksFile:
    return TasksFile(tasks=tasks)


class TestValidatePipelineMissingTask:
    def test_missing_task_raises(self) -> None:
        pipeline = PipelineConfig(
            description="t", stages=[PipelineStage(name="Stage A", task="ghost-task")]
        )
        tasks = _tasks(**{"real-task": TaskConfig(description="d")})

        with pytest.raises(ValueError, match="missing task"):
            validate_pipeline(pipeline, tasks)


class TestValidateRolesUnknownRole:
    def test_unknown_role_raises_actionable_error(self) -> None:
        tasks = _tasks(**{"task-a": TaskConfig(description="d", role="nonexistent_role")})

        with pytest.raises(ValueError) as exc_info:
            validate_roles(tasks)

        message = str(exc_info.value)
        assert "task-a" in message, "error must name the offending task"
        assert "nonexistent_role" in message, "error must name the unknown role"
        assert "roles.yaml" in message, "error must point at roles.yaml/example"

    def test_validate_pipeline_surfaces_unknown_role_not_bare_keyerror(self) -> None:
        pipeline = PipelineConfig(
            description="t", stages=[PipelineStage(name="Stage A", task="task-a")]
        )
        tasks = _tasks(**{"task-a": TaskConfig(description="d", role="nonexistent_role")})

        with pytest.raises(ValueError, match="nonexistent_role"):
            validate_pipeline(pipeline, tasks)

    def test_known_role_does_not_raise(self) -> None:
        tasks = _tasks(**{"task-a": TaskConfig(description="d", role="developer")})
        validate_roles(tasks)  # developer always exists (code default or roles.yaml)

    def test_task_with_no_role_is_never_flagged(self) -> None:
        tasks = _tasks(**{"task-a": TaskConfig(description="d")})
        validate_roles(tasks)  # role=None -- nothing to validate, must not raise


class TestValidatePipelineAgainstRealShippedConfig:
    """This repo's own root roles.yaml/tasks.yaml/pipelines.yaml (the
    "company" dogfood pipeline) must keep validating cleanly -- the
    full-replace roles.yaml loader is unchanged, so every role the shipped
    tasks.yaml references (ceo, chief_of_staff, cto, developer, reviewer,
    ciso, qa, documentation) is still defined."""

    def test_default_pipeline_still_validates(self) -> None:
        from hivepilot.services.project_service import load_pipelines, load_tasks

        pipeline = load_pipelines().pipelines["default"]
        tasks = load_tasks()
        validate_pipeline(pipeline, tasks)  # must not raise

    def test_company_pipeline_still_validates(self) -> None:
        from hivepilot.services.project_service import load_pipelines, load_tasks

        pipeline = load_pipelines().pipelines["company"]
        tasks = load_tasks()
        validate_pipeline(pipeline, tasks)  # must not raise
