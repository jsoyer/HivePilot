from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RunnerDefinition(BaseModel):
    name: str | None = None
    kind: Literal["claude", "shell", "langchain", "internal", "codex", "gemini", "opencode", "ollama", "api", "container"]
    command: str | None = None
    model: str | None = None
    agent: str | None = None
    append_prompt: str | None = None
    timeout_seconds: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class TaskStep(BaseModel):
    name: str
    runner: str
    runner_ref: str | None = None
    prompt_file: str | None = None
    command: str | None = None
    allow_failure: bool = False
    append_prompt: str | None = None
    timeout_seconds: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    knowledge_files: list[str] = Field(default_factory=list)
    secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_fields(self) -> TaskStep:
        if not self.runner:
            raise ValueError(f"Step '{self.name}' requires a runner")
        return self


class GitActions(BaseModel):
    commit: bool = False
    push: bool = False
    create_pr: bool = False
    commit_message: str | None = None
    pr_title: str | None = None
    pr_body_file: str | None = None
    branch_prefix: str = "hivepilot"


class TaskConfig(BaseModel):
    description: str
    engine: Literal["native", "langgraph", "crewai"] = "native"
    graph: str | None = None
    crew: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    git: GitActions = Field(default_factory=GitActions)
    options: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class ProjectConfig(BaseModel):
    path: Path
    description: str | None = None
    claude_md: str | None = None
    default_branch: str = "main"
    owner_repo: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def expand_path(self) -> ProjectConfig:
        self.path = self.path.expanduser().resolve()
        return self


class PipelineStage(BaseModel):
    name: str
    task: str


class PipelineConfig(BaseModel):
    description: str
    stages: list[PipelineStage] = Field(default_factory=list)


class ProjectsFile(BaseModel):
    projects: dict[str, ProjectConfig]


class TasksFile(BaseModel):
    runners: dict[str, RunnerDefinition] = Field(default_factory=dict)
    tasks: dict[str, TaskConfig]

    @model_validator(mode="after")
    def inject_runner_names(self) -> TasksFile:
        for name, runner in self.runners.items():
            runner.name = runner.name or name
        return self


class PipelinesFile(BaseModel):
    pipelines: dict[str, PipelineConfig] = Field(default_factory=dict)
