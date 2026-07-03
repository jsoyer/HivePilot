from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.models import GroupsFile, PipelinesFile, ProjectsFile, TasksFile


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


def load_groups(path: Path | None = None) -> GroupsFile:
    resolved = settings.resolve_config_path(path or settings.groups_file)
    return GroupsFile.model_validate(_read_yaml(resolved))


def resolve_targets(name: str) -> list[str]:
    """Expand a group name to its component projects; a plain project name returns
    ``[name]``. Group lookup takes precedence over a same-named project."""
    groups = load_groups().groups
    if name in groups:
        return list(groups[name].components)
    return [name]
