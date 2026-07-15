from __future__ import annotations

from pathlib import Path
from typing import List

from hivepilot.models import TaskConfig
from hivepilot.registry import RunnerRegistry
from hivepilot.services.project_service import load_pipelines, load_projects, load_tasks
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class LintError(RuntimeError):
    pass


def lint_configuration() -> List[str]:
    projects = load_projects()
    tasks = load_tasks()
    pipelines = load_pipelines()
    errors: List[str] = []

    for name, task in tasks.tasks.items():
        errors.extend(_lint_task(name, task))

    for pipeline_name, pipeline in pipelines.pipelines.items():
        for stage in pipeline.stages:
            if stage.task not in tasks.tasks:
                errors.append(f"Pipeline '{pipeline_name}' references unknown task '{stage.task}'")

    for name, project in projects.projects.items():
        if not project.path.exists():
            errors.append(f"Project '{name}' path does not exist: {project.path}")

    return errors


def _lint_task(name: str, task: TaskConfig) -> List[str]:
    """Lint a task's steps.

    Runner kinds are checked against the *live* registry
    (``RunnerRegistry.known_kinds()``, backed by ``RUNNER_MAP``) rather than
    a hardcoded set — this catches advertised-but-unregistered kinds (e.g.
    the historical ``"api"`` orphan; see roadmap Phase 26a) and correctly
    accepts plugin-contributed kinds registered at runtime.
    """
    errors: List[str] = []
    known_runners = RunnerRegistry.known_kinds()
    for step in task.steps:
        if step.prompt_file:
            path = Path(step.prompt_file)
            if not path.exists():
                errors.append(f"Task '{name}' step '{step.name}' missing prompt file {path}")
        if step.runner not in known_runners and not step.runner_ref:
            errors.append(
                f"Task '{name}' step '{step.name}' references unknown runner '{step.runner}' (missing runner_ref?)"
            )
    return errors
