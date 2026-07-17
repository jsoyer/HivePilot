from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, get_args

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


# THE single reasoning-effort type — the one definition used everywhere a
# role/stage/policy/step can request an effort level (RunnerDefinition,
# TaskStep, PipelineStage, PipelineConfig here; Role in hivepilot/roles.py).
# This reconciles two independently-shipped effort systems (the stage/pipeline
# model+effort knob and the per-role/step Claude effort knob) into ONE closed
# set. Deliberately a closed `Literal` (not a plain `str`, unlike `RunnerKind`)
# — effort is an internal HivePilot concept mapped per-runner (Claude ->
# `MAX_THINKING_TOKENS`, Codex -> `-c model_reasoning_effort=<level>`), not an
# external plugin-contributed namespace, so a typo fails loudly at config-load
# time. `"xhigh"` is a HivePilot superset level between `high` and `max` (the
# Claude runner maps it to a token budget in that gap; the Codex runner passes
# the literal string through).
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]

RunnerKind = str

# The legal effort strings as a tuple, DERIVED from `EffortLevel` so there is a
# single source of truth (pydantic validates the `EffortLevel`-typed fields
# directly; this tuple + `validate_effort` are the programmatic guard for any
# non-pydantic caller, and the Claude-runner token-budget map in
# `hivepilot.runners.claude_runner.EFFORT_TOKEN_MAP` must cover exactly it).
EFFORT_LEVELS: tuple[str, ...] = get_args(EffortLevel)


def validate_effort(value: str | None) -> str | None:
    """Programmatic guard for an effort level outside a pydantic field.
    `None` means "no effort declared" (byte-identical to pre-effort behaviour)
    and is always allowed; any non-`None` value must be one of `EFFORT_LEVELS`
    (i.e. a valid `EffortLevel`)."""
    if value is not None and value not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of {EFFORT_LEVELS} or None, got {value!r}")
    return value


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
    "container",
    "cursor",
    "vibe",
    # Sprint 2 (runner-defaults-plugins-mode PRD): the only new built-in
    # agent kind (API-only). gemini/opencode/ollama were removed from this
    # tuple — they moved OUT of RUNNER_MAP's built-in registration and into
    # default-on, PATH-gated plugins (plugins/gemini.py / opencode.py /
    # ollama.py); they are no longer unconditionally present in RUNNER_MAP,
    # so listing them here would violate this tuple's own documented
    # invariant (see the NOTE above and
    # tests/test_models.py::test_known_runner_kinds_all_have_runner_map_entries).
    "openrouter",
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
    # Resolved reasoning-effort level — carries the orchestrator's authoritative
    # `policy > stage > role > runner-default` precedence result (see
    # `hivepilot.roles.resolve_stage_dispatch`) through to the runner. `None`
    # means "no effort configured anywhere in the chain"; each runner decides
    # its own unset-default (Codex falls back to `"medium"`; Claude injects no
    # `MAX_THINKING_TOKENS`). Runners with no effort concept ignore this field.
    # This is the value each runner treats as authoritative — a per-step
    # `TaskStep.effort` only applies as a fallback when this is `None` (see
    # `hivepilot.runners.base.resolve_runner_effort`).
    effort: EffortLevel | None = None
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
    # Per-step reasoning-effort override. In the unified precedence this is a
    # FALLBACK beneath the orchestrator-resolved `RunnerDefinition.effort`
    # (policy > stage > role): it only takes effect when nothing was resolved
    # upstream, so a step can never silently override a stage- or policy-
    # mandated effort (see `hivepilot.runners.base.resolve_runner_effort`).
    # None (default) means no override -- byte-identical to pre-effort behaviour.
    effort: EffortLevel | None = None

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
    # Stage-level model + reasoning-effort overrides (roles-model-effort-
    # config-owned PRD, Sprint 1). Both default to None -- "inherit the
    # pipeline-level default (`PipelineConfig.model`/`.effort`), which itself
    # falls back to the role binding" -- so a pipeline that sets neither
    # dispatches byte-identically to before these fields existed. See
    # `resolve_stage_model`/`resolve_effort` for the pipeline-vs-stage
    # precedence, and `hivepilot.roles.resolve_stage_dispatch` for the full
    # `policy > stage > role > runner-default` chain the orchestrator applies
    # on top of these two resolved values.
    model: str | None = None
    effort: EffortLevel | None = None
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
    # Pipeline-wide default model + reasoning-effort — the same "stage
    # overrides pipeline overrides nothing" shape as `mode`/`resolve_mode`,
    # except there is no hardcoded non-None fallback (unlike `mode`'s
    # `"cli"`): a pipeline that sets neither leaves both fully unset, which
    # `hivepilot.roles.resolve_stage_dispatch` then falls back to the role
    # binding / runner-default for.
    model: str | None = None
    effort: EffortLevel | None = None
    stages: list[PipelineStage] = Field(default_factory=list)


def resolve_mode(pipeline: PipelineConfig, stage: PipelineStage) -> Literal["cli", "api"]:
    """Resolve the effective execution mode for *stage* within *pipeline*.

    Precedence: an explicit ``stage.mode`` wins over the pipeline-wide
    ``pipeline.mode``, which in turn falls back to the ``"cli"`` default. This
    is the single source of truth the orchestrator uses to decide whether a
    stage's agent runners take their CLI path or their provider-API path.
    """
    return stage.mode or pipeline.mode or "cli"


def resolve_stage_model(pipeline: PipelineConfig, stage: PipelineStage) -> str | None:
    """Resolve the pipeline/stage-level model default for *stage*.

    Precedence: an explicit ``stage.model`` wins over the pipeline-wide
    ``pipeline.model``; ``None`` when neither is set (the orchestrator then
    falls back to the role binding / policy override via
    ``hivepilot.roles.resolve_stage_dispatch``). Mirrors ``resolve_mode``'s
    stage-over-pipeline precedence, minus a hardcoded final default.
    """
    return stage.model or pipeline.model


def resolve_effort(pipeline: PipelineConfig, stage: PipelineStage) -> EffortLevel | None:
    """Resolve the pipeline/stage-level reasoning-effort default for *stage*.

    Precedence: an explicit ``stage.effort`` wins over the pipeline-wide
    ``pipeline.effort``; ``None`` when neither is set. This is only the
    pipeline-vs-stage layer -- the orchestrator threads this result into
    ``hivepilot.roles.resolve_stage_dispatch`` as ``stage_effort``, where a
    policy ``role_overrides`` entry can still outrank it.
    """
    return stage.effort or pipeline.effort


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
