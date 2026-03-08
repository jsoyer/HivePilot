from __future__ import annotations

from pathlib import Path
from typing import List

from hivepilot.models import TaskConfig
from hivepilot.services.project_service import load_projects, load_tasks, load_pipelines
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


KNOWN_RUNNERS = {"claude", "shell", "langchain", "internal", "codex", "gemini", "opencode", "ollama", "api", "container"}


def _lint_task(name: str, task: TaskConfig) -> List[str]:
    errors: List[str] = []
    for step in task.steps:
        if step.prompt_file:
            path = Path(step.prompt_file)
            if not path.exists():
                errors.append(f"Task '{name}' step '{step.name}' missing prompt file {path}")
        if step.runner not in KNOWN_RUNNERS and not step.runner_ref:
            errors.append(f"Task '{name}' step '{step.name}' references unknown runner '{step.runner}' (missing runner_ref?)")
    return errors
