from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _dedup_ordered(value: list[str] | None) -> list[str] | None:
    """Dedup a list of strings while preserving first-occurrence order.

    ``None`` passes through unchanged — absence means "dormant", not "empty
    list" (see `TaskStep.skills` / `PipelineStage.skills`)."""
    if value is None:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


RunnerKind = str

# Built-in kinds, for docs/help/typing only — NOT enforced at runtime; see RunnerRegistry.
#
# NOTE: this tuple must stay a subset of the *actually registered* kinds in
# hivepilot.registry.RUNNER_MAP (verified by
# tests/test_models.py::test_known_runner_kinds_all_have_runner_map_entries).
# Do NOT add a kind here unless a runner class is registered for it — an
# advertised-but-unregistered kind is exactly the "api" orphan bug fixed in
# roadmap Phase 26a (a config with that kind used to raise a bare KeyError
# at resolve time). CLI/orchestrator validation intentionally checks the
# live registry (RUNNER_MAP / RunnerRegistry.known_kinds()), not this tuple,
# so it also accepts plugin-contributed kinds that aren't listed here.
KNOWN_RUNNER_KINDS: tuple[str, ...] = (
    "claude",
    "shell",
    "langchain",
    "internal",
    "codex",
    "gemini",
    "opencode",
    "ollama",
    "container",
    "cursor",
    "vibe",
    "terraform",
    "opentofu",
    "pulumi",
    "kubectl",
    "ansible",
    "helm",
    "kustomize",
    "packer",
    "salt",
    "chef",
    "puppet",
)


class RunnerDefinition(BaseModel):
    name: str | None = None
    kind: RunnerKind
    command: str | None = None
    model: str | None = None
    agent: str | None = None
    append_prompt: str | None = None
    timeout_seconds: int | None = None
    host: str | None = None  # SSH host/alias to run this agent on (None = local)
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
    # Step-level destructive-operation approval gate (Phase 17a-B): when True,
    # the orchestrator pauses the task for human approval before this step
    # runs, regardless of whether the runner itself declares the operation
    # destructive. A runner-declared destructive operation gates even when
    # this flag is left False (see `hivepilot.orchestrator.step_requires_approval`).
    require_approval: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    knowledge_files: list[str] = Field(default_factory=list)
    secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Skill plugin type (skill-plugin-type PRD, Sprint 3): ordered, deduped
    # names of plugin-contributed skills this step wants applied. Default
    # None -- dormant, byte-identical when absent. Cross-referenced against
    # `PluginManager.list_skills()` (unknown name -> hard validation error)
    # and gated by each skill's optional `min_role` -- see
    # `hivepilot/services/config_validation.py`.
    skills: list[str] | None = None

    @field_validator("skills")
    @classmethod
    def _dedup_skills(cls, v: list[str] | None) -> list[str] | None:
        return _dedup_ordered(v)

    @model_validator(mode="after")
    def validate_fields(self) -> TaskStep:
        if not self.runner:
            raise ValueError(f"Step '{self.name}' requires a runner")
        return self


class GitActions(BaseModel):
    commit: bool = False
    push: bool = False
    create_pr: bool = False
    draft: bool = False  # open create_pr's PR as a draft (gh pr create --draft)
    merge_pr: bool = False  # Jules' autonomous final approval: merge the branch's PR
    promote_pr: bool = False  # release gate: mark an existing draft PR ready for review
    merge_method: str = "merge"  # merge | squash | rebase
    commit_message: str | None = None
    pr_title: str | None = None
    pr_body_file: str | None = None
    branch_prefix: str = "hivepilot"


class TaskConfig(BaseModel):
    description: str
    role: str | None = None
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
    # Named secret catalog: NAME -> {source, ...} spec (same shape the
    # SecretResolver consumes). Referenced from `env` values via the
    # ${secret:NAME} syntax and resolved lazily at step-assembly time.
    secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def expand_path(self) -> ProjectConfig:
        self.path = self.path.expanduser().resolve()
        return self


class PipelineStage(BaseModel):
    name: str
    task: str
    # Execution mode for this stage's agent runners. None means "inherit the
    # pipeline-level default" (see `PipelineConfig.mode` / `resolve_mode`);
    # an explicit value overrides the pipeline default for this stage only.
    mode: Literal["cli", "api"] | None = None
    pause_before: bool = False  # pause pipeline for human plan approval before this stage
    commits_vault: bool = False  # stage triggers a vault changelog commit after execution
    # Stage scoping (PRD A1): restrict this stage to a subset of the run's
    # selected components. Both are additive/optional — a stage with neither
    # set always runs (backward compatible with existing pipelines).
    only_components: list[str] | None = None
    only_tags: list[str] | None = None
    # When True, a failed stage does not fail-fast the run (the pipeline
    # continues to the next stage instead of breaking).
    continue_on_failure: bool = False
    # Skill plugin type (skill-plugin-type PRD, Sprint 3): ordered, deduped
    # names of plugin-contributed skills this stage wants applied. Default
    # None -- dormant, byte-identical when absent. See `TaskStep.skills` for
    # the identical semantics and `hivepilot/services/config_validation.py`
    # for the fail-closed cross-reference + `min_role` check.
    skills: list[str] | None = None

    @field_validator("skills")
    @classmethod
    def _dedup_skills(cls, v: list[str] | None) -> list[str] | None:
        return _dedup_ordered(v)


class PipelineConfig(BaseModel):
    description: str
    # Pipeline-wide default execution mode for agent runners. `cli` (the
    # default) drives each agent through its command-line binary — byte-
    # identical to pre-mode behaviour. `api` routes API-capable agent runners
    # (claude / prompt-cli) through the provider's HTTP API instead. A stage
    # may override this via `PipelineStage.mode` (see `resolve_mode`).
    mode: Literal["cli", "api"] = "cli"
    stages: list[PipelineStage] = Field(default_factory=list)


def resolve_mode(pipeline: PipelineConfig, stage: PipelineStage) -> Literal["cli", "api"]:
    """Resolve the effective execution mode for *stage* within *pipeline*.

    Precedence: an explicit ``stage.mode`` wins over the pipeline-wide
    ``pipeline.mode``, which in turn falls back to the ``"cli"`` default. This
    is the single source of truth the orchestrator uses to decide whether a
    stage's agent runners take their CLI path or their provider-API path.
    """
    return stage.mode or pipeline.mode or "cli"


class ProjectsFile(BaseModel):
    projects: dict[str, ProjectConfig]


class Group(BaseModel):
    """A product made of many component repos (e.g. Acme → acme-api, ...)."""

    description: str | None = None
    hub: str | None = None  # project where group-level planning runs (from E2)
    components: list[str] = Field(default_factory=list)
    # tag -> component names, used to resolve PipelineStage.only_tags.
    tags: dict[str, list[str]] = Field(default_factory=dict)
    # Monorepo group (opt-in, default False): `components`/`tags` are pure
    # scoping labels — they gate WHICH stages run via only_components/only_tags
    # (_stage_should_skip) exactly like a multi-repo group, but every stage
    # that DOES run executes once at `hub` (git + execution), never fanned out
    # per component. Component labels are never resolved as projects in this
    # mode. Default False keeps every existing multi-repo group byte-identical.
    single_repo: bool = False

    @model_validator(mode="after")
    def require_hub_when_single_repo(self) -> Group:
        if self.single_repo and not self.hub:
            raise ValueError("Group.single_repo=True requires a non-empty 'hub'")
        return self


class GroupsFile(BaseModel):
    groups: dict[str, Group] = Field(default_factory=dict)


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
