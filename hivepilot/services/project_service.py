from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.models import PipelinesFile, ProjectsFile, TasksFile


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_projects(path: Path | None = None) -> ProjectsFile:
    resolved = settings.resolve_config_path(path or settings.projects_file)
    return ProjectsFile.model_validate(_read_yaml(resolved))


def load_tasks(path: Path | None = None) -> TasksFile:
    resolved = settings.resolve_config_path(path or settings.tasks_file)
    return TasksFile.model_validate(_read_yaml(resolved))


def load_pipelines(path: Path | None = None) -> PipelinesFile:
    resolved = settings.resolve_config_path(path or settings.pipelines_file)
    return PipelinesFile.model_validate(_read_yaml(resolved))
