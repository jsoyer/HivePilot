from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from hivepilot.config import Settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep


@dataclass(slots=True)
class RunnerPayload:
    project_name: str
    project: ProjectConfig
    task_name: str
    step: TaskStep
    metadata: dict[str, Any]


class BaseRunner(Protocol):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        ...
