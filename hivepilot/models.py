from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GitActions(BaseModel):
    commit: bool = False
    push: bool = False
    create_pr: bool = False
    commit_message: str | None = None
    pr_title: str | None = None
    pr_body_file: str | None = None
    branch_prefix: str = "hivepilot"


class TaskStep(BaseModel):
    name: str
    runner: Literal["claude", "shell"]
    prompt_file: str | None = None
    command: str | None = None
    agent: str | None = None
    model: str | None = None
    append_prompt: str | None = None
    allow_failure: bool = False
    timeout_seconds: int = 3600

    @model_validator(mode="after")
    def _validate_runner(self) -> TaskStep:
        if self.runner == "claude" and not self.prompt_file:
            raise ValueError(f"Claude step '{self.name}' requires prompt_file")
        if self.runner == "shell" and not self.command:
            raise ValueError(f"Shell step '{self.name}' requires command")
        return self


class TaskConfig(BaseModel):
    description: str
    steps: list[TaskStep] = Field(default_factory=list)
    git: GitActions = Field(default_factory=GitActions)


class ProjectConfig(BaseModel):
    path: Path
    description: str | None = None
    claude_md: str | None = None
    default_branch: str = "main"
    owner_repo: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ProjectsFile(BaseModel):
    projects: dict[str, ProjectConfig]


class TasksFile(BaseModel):
    tasks: dict[str, TaskConfig]
