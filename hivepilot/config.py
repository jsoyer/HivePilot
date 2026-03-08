from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from hivepilot.models import ProjectsFile, TasksFile


class ConfigError(RuntimeError):
    pass



def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing configuration file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Invalid YAML root in {path}. Expected a mapping.")
    return data



def load_projects(path: Path) -> ProjectsFile:
    try:
        return ProjectsFile.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid projects config in {path}: {exc}") from exc



def load_tasks(path: Path) -> TasksFile:
    try:
        return TasksFile.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid tasks config in {path}: {exc}") from exc
