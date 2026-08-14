from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


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
    "container",
    # `vibe` removed for the same reason as codex/cursor/gemini/opencode/
    # ollama: it moved OUT of RUNNER_MAP's built-in registration into a
    # default-on, PATH-gated plugin (plugins/vibe.py), so it is no longer
    # unconditionally present in RUNNER_MAP and listing it here would
    # violate this tuple's own invariant. See tests/test_vibe.py.
    # Sprint 2 (runner-defaults-plugins-mode PRD): the only new built-in
    # agent kind (API-only). gemini/opencode/ollama were removed from this
    # tuple — they moved OUT of RUNNER_MAP's built-in registration and into
    # default-on, PATH-gated plugins (plugins/gemini.py / opencode.py /
    # ollama.py); they are no longer unconditionally present in RUNNER_MAP,
    # so listing them here would violate this tuple's own documented
    # invariant (see the NOTE above and
    # tests/test_models.py::test_known_runner_kinds_all_have_runner_map_entries).
    # codex-cursor-plugins migration: codex/cursor removed from this tuple
    # for the same reason — they moved OUT of RUNNER_MAP's built-in
    # registration and into default-on, PATH-gated plugins (plugins/codex.py
    # / plugins/cursor.py); see tests/test_codex.py / tests/test_cursor.py.
    "openrouter",
    "terraform",
    "opentofu",
    "pulumi",
    "atlantis",
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
    # ---- inline-repo-instructions PRD ----
    # Generic list of repository instruction/context files (e.g. AGENTS.md,
    # AGENT-GOVERNANCE.md, .cursorrules, .windsurfrules, GEMINI.md) whose
    # CONTENT is resolved and inlined into the agent prompt by
    # `hivepilot.services.repo_instructions.build_repo_instructions_section`
    # -- see that module for resolution/redaction/size-cap behavior.
    # Deliberately named for what it IS (repository instruction/context
    # files), not after any one vendor's convention (unlike `claude_md`,
    # kept above for backward compatibility -- both fields can be set
    # independently and are injected together, `claude_md` first). `None`
    # (the default) means "no additional files" -- zero behavior change for
    # every existing config that doesn't declare this field.
    instruction_files: list[str] | None = None
    default_branch: str = "main"
    owner_repo: str | None = None
    # ---- forge plugin type (forge-plugin-type-phase1 PRD, Sprint 1) ----
    # Which git-forge provider (see hivepilot.forges) this project's repo
    # lives on. Defaults to "github" -- the only forge shipped in Phase 1 --
    # so every pre-existing project (no `forge:` key in projects.yaml) is
    # completely unaffected. `forge_base_url` is additive/inert in Phase 1
    # (no provider reads it yet); it exists now so a later self-hosted
    # Forgejo/GitLab provider can land without another ProjectConfig schema
    # change.
    forge: str = "github"
    forge_base_url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    # Named secret catalog: NAME -> {source, ...} spec (same shape the
    # SecretResolver consumes). Referenced from `env` values via the
    # ${secret:NAME} syntax and resolved lazily at step-assembly time.
    secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # ---- Monorepo modules (monorepo-modules PRD) ----
    # Module name -> subpath RELATIVE to `path`, e.g.
    # `{"detection-core": "apps/detection-core"}`. A run can target
    # `<project>/<module>` (see `hivepilot.services.project_service.
    # resolve_project_target`) to scope the agent's working directory to
    # `path / modules[module]` instead of the repo root. Additive, defaults
    # to empty -> zero behaviour change for a project that doesn't declare
    # `modules` (every existing project is unaffected).
    modules: dict[str, str] = Field(default_factory=dict)
    # Module names (a subset of `modules`' keys) that are UI surfaces --
    # pure metadata for callers (e.g. a future "which modules have a UI"
    # filter); not consumed anywhere in this sprint.
    ui_surfaces: list[str] = Field(default_factory=list)
    # ---- Per-project Obsidian vault (per-project-vault PRD) ----
    # Where THIS project's HivePilot artifacts are written (and read back
    # from by the `obsidian` plugin's `recall`). `None` (the default, and an
    # explicit `obsidian_vault: null`) means "inherit the global
    # `Settings.obsidian_vault`" -- byte-identical to before this field
    # existed, so every pre-existing deployment is unaffected until someone
    # opts in.
    #
    # Exists because HivePilot is a generic engine whose unit of
    # configuration is a PIPELINE, and several pipelines routinely coexist on
    # one host (the operator's own HivePilot work vs. a product pipeline). A
    # single global vault cannot express that.
    #
    # Fail-closed semantics, enforced by `validate_obsidian_vault` below and
    # documented in full in `hivepilot.services.obsidian_vault_resolver`:
    # an EMPTY/whitespace-only value and a RELATIVE path are both REJECTED at
    # load time. Empty must never silently mean "use the global" (that would
    # route artifacts back into the very vault the operator was moving away
    # from), and a relative path is the cwd-silo bug class that produced
    # three divergent `state.db` files on the operator's box.
    obsidian_vault: Path | None = None

    @field_validator("forge")
    @classmethod
    def validate_forge(cls, v: str) -> str:
        """Fail-closed (SECURITY/correctness): an unknown `forge` name must
        raise at config-load time -- NEVER silently fall back to GitHub.
        Mirrors the fail-closed posture used elsewhere in this module (see
        `validate_modules` below) and the unknown-runner-kind error
        `resolve_runner_class` raises, just enforced earlier (at model
        validation, since -- unlike an agent runner plugin, which may be
        disabled or missing its CLI binary at runtime -- `FORGE_MAP`'s
        builtin ("github") is always registered by the time any
        `ProjectConfig` is constructed; see `hivepilot/forges/__init__.py`).

        Local import (not at module top) to avoid a
        `hivepilot.models` <-> `hivepilot.forges` import cycle --
        `hivepilot.forges.provider`/`.github` both import `ProjectConfig`
        from this module for type hints, so importing `hivepilot.forges`
        back at THIS module's top level would be circular. By the time this
        validator runs (a `ProjectConfig(...)` is being constructed, always
        after import-time), both modules are already fully loaded, so the
        lazy import here is safe.
        """
        from hivepilot.forges import FORGE_MAP

        if v not in FORGE_MAP:
            raise ValueError(f"Unknown forge {v!r}; available: {sorted(FORGE_MAP)}")
        return v

    @field_validator("obsidian_vault", mode="before")
    @classmethod
    def validate_obsidian_vault(cls, v: Any) -> Any:
        """Fail closed on the two ways a per-project vault override silently
        sends artifacts to the wrong place.

        `mode="before"` so the RAW value is inspected: pydantic coerces `""`
        to `Path(".")`, which would otherwise be indistinguishable from a
        deliberate (still wrong) relative path and produce a misleading error.

        1. EMPTY / whitespace-only -> reject. Never "fall back to the global
           vault": an empty value on a routing decision is always a config
           mistake (a typo, or an unexpanded `${VAULT}` template), and
           treating it as "no constraint" would silently route this project's
           artifacts back into the shared vault the operator was explicitly
           moving away from. This is the documented recurring bug class in
           this codebase (an empty value read as "no constraint" -> the gate
           fails OPEN); on a destination, empty means reject.
        2. RELATIVE -> reject. A relative vault path resolves against
           whatever cwd the daemon happens to have -- the same cwd-silo bug
           class that produced three divergent `state.db` files on the
           operator's box. `~` is expanded first, so `~/vault` is accepted.

        `None` (absent, or an explicit `obsidian_vault: null`) passes through
        untouched and means "inherit the global setting".
        """
        if v is None:
            return None
        text = str(v).strip()
        if not text:
            raise ValueError(
                "ProjectConfig.obsidian_vault must not be empty -- an empty override "
                "is never interpreted as 'use the global vault'. Remove the key "
                "entirely to inherit HIVEPILOT_OBSIDIAN_VAULT, or set a real path."
            )
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ValueError(
                f"ProjectConfig.obsidian_vault = {text!r} must be an absolute path "
                "(or start with '~/'). A relative vault path resolves against the "
                "daemon's current working directory, so the same config would write "
                "artifacts to a different place depending on how HivePilot was "
                "started."
            )
        return path.resolve()

    @field_validator("instruction_files")
    @classmethod
    def _dedup_instruction_files(cls, v: list[str] | None) -> list[str] | None:
        """Same dedup-preserving-order posture as `TaskStep.skills` — a
        duplicate entry (e.g. someone re-listing `claude_md`'s file in
        `instruction_files` too) collapses to one, first occurrence wins."""
        return _dedup_ordered(v)

    @model_validator(mode="after")
    def expand_path(self) -> ProjectConfig:
        self.path = self.path.expanduser().resolve()
        return self

    @model_validator(mode="after")
    def validate_modules(self) -> ProjectConfig:
        """SECURITY: every `modules` subpath must resolve to a location
        INSIDE `path` — reject (fail closed, raise) any subpath that is
        absolute or escapes via `..` (e.g. `../../etc`), so a malicious or
        typo'd `modules:` entry can never let an agent operate outside the
        project's repo checkout. Validated here at load time (config
        parsing), not deferred to run time, so a bad entry fails loudly the
        moment projects.yaml is loaded — the same fail-closed posture the
        rest of this module already applies to config-layer values (see
        `DebateConfig`).

        Checked against the RESOLVED path (`.resolve()`), never a raw
        string/substring check — a naive `".." in subpath` guard is easy to
        bypass (e.g. `foo/../../bar`) and would still fail-open.
        """
        if not self.modules:
            return self
        root = self.path  # already resolved by `expand_path` (runs first)
        for name, subpath in self.modules.items():
            candidate = Path(subpath)
            if candidate.is_absolute():
                raise ValueError(
                    f"ProjectConfig.modules['{name}'] = {subpath!r} is an absolute "
                    f"path — module subpaths must be relative to the project's "
                    f"path and stay inside it."
                )
            resolved = (root / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"ProjectConfig.modules['{name}'] = {subpath!r} escapes "
                    f"outside the project path {root} — module subpaths must "
                    f"resolve to a location inside the project's checkout."
                ) from exc
        return self


# ---- Debate/consensus YAML config (debate-judge-pipeline-yaml PRD, Sprint 1) ----
# Pure config-layer surface for the debate judge / challenge arbiter (see
# `Settings.enable_debate_judge`/`.enable_challenge_arbiter`/`.judge_runner`/
# `.judge_model`/`.judge_confidence_threshold` in hivepilot/config.py, the
# fail-closed PR gate they feed via `Orchestrator._governing_verdict`). This
# sprint only adds the pipeline/stage-level YAML override surface + the
# resolver that reconciles it with the global floor — no orchestrator wiring
# yet, so a `debate:` block in pipelines.yaml is inert until a later sprint
# threads `resolve_debate_config` into the orchestrator.
#
# SECURITY: this is the "empty-value-fail-open" bug class HivePilot has hit
# before (see error-registry). A `debate:` block must never be able to turn a
# global fail-closed gate OFF: enable flags are OR'd (strengthen-only, a
# pipeline/stage `False`/absent can never override a floor `True`), and the
# confidence threshold must be validated `(0, 1]` at load time so a bad value
# (0, negative, >1, NaN, inf) is rejected before it can ever reach the gate.
# ---- Adversarial review facet (adversarial-review-thin-layer PRD, Sprint 1) ----
# Review is NOT a parallel concept -- it rides the existing `debate:` block
# because a review round IS a debate round from the orchestrator's point of
# view (same runner/model dispatch, same stage/pipeline override shape).
# Two fields ride along on `DebateConfig`: `reviewers` (who participates) and
# `review_target` (what they review -- an internal artifact vs. a GitHub
# PR). There is deliberately NO `Settings`/global floor for review (unlike
# `enable_judge`/`enable_challenge_arbiter`): review is purely a per-
# pipeline/per-stage opt-in facet, so `DebateFloor` and `Settings`
# (`hivepilot/config.py`) are untouched by this sprint -- the floor-
# controlled OR-strengthen precedence in `resolve_debate_config` stays
# scoped to the judge/arbiter enable flags only.
#
# FAIL-CLOSED: a `review_target` requesting a review with zero reviewers is
# the same "empty-value-fail-open" bug class as `confidence_threshold`/
# `enable_judge` above. Guarded twice: (1) at load, `DebateConfig` rejects a
# `review_target` set alongside an explicit `reviewers: []` in the SAME
# block (model validator below); (2) at resolve time, because (1) cannot see
# a cross-layer combination (e.g. pipeline sets `review_target` while stage
# independently sets `reviewers: []`), `resolve_debate_config` re-checks the
# fully resolved values and raises if `review_target` is set but the
# resolved `reviewers` list is empty.
class DebateConfig(BaseModel):
    enable_judge: bool | None = None
    enable_arbiter: bool | None = None
    runner: str | None = None
    model: str | None = None
    confidence_threshold: float | None = None
    # Adversarial review facet (Sprint 1). Both default to None -- dormant,
    # byte-identical to pre-review behaviour. See the module comment above
    # for the "review rides the debate block" design decision and the
    # fail-closed contract.
    reviewers: list[str] | None = None
    review_target: Literal["internal", "github_pr"] | None = None

    @field_validator("confidence_threshold")
    @classmethod
    def _validate_confidence_threshold(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not math.isfinite(v) or not (0 < v <= 1):
            raise ValueError(
                f"debate.confidence_threshold must be a finite number in (0, 1], got {v!r}"
            )
        return v

    @field_validator("runner", "model")
    @classmethod
    def _reject_blank_override(cls, v: str | None, info: ValidationInfo) -> str | None:
        # A present-but-blank runner/model ("" or whitespace-only) is falsy and
        # would silently fall through to the pipeline/floor value in
        # `resolve_debate_config` -- indistinguishable from "unset", which hides
        # a config typo. Reject it at load so a present value is always
        # meaningful (mirrors the confidence_threshold "reject at load"
        # contract). Omit the key / use None to inherit the floor.
        if v is not None and not v.strip():
            raise ValueError(
                f"debate.{info.field_name} must be a non-empty string when set "
                "(omit the key to inherit the pipeline/floor value)"
            )
        return v

    @field_validator("reviewers")
    @classmethod
    def _reject_blank_reviewer(cls, v: list[str] | None) -> list[str] | None:
        # A blank/whitespace-only reviewer name is the same class of typo as
        # a blank runner/model above -- reject it at load rather than let it
        # silently participate (or silently fail to resolve to anyone) later.
        if v is None:
            return None
        for name in v:
            if not name.strip():
                raise ValueError(
                    "debate.reviewers must not contain blank/whitespace-only names "
                    "(use a real reviewer identifier, or omit/empty the entry instead)"
                )
        return v

    @model_validator(mode="after")
    def _reject_review_target_without_reviewers(self) -> DebateConfig:
        # Catches the SAME-BLOCK case only (e.g. one YAML `debate:` block sets
        # both `review_target: github_pr` and `reviewers: []`). A cross-block
        # combination -- e.g. pipeline sets `review_target`, stage
        # independently sets `reviewers: []` -- can't be seen here since each
        # `DebateConfig` instance validates in isolation; that case is caught
        # at resolve time by `resolve_debate_config` instead (see there).
        if (
            self.review_target is not None
            and self.reviewers is not None
            and len(self.reviewers) == 0
        ):
            raise ValueError(
                "debate.review_target requires at least one reviewer: "
                "reviewers=[] together with a review_target set is rejected at load "
                "(fail-closed) -- list at least one reviewer, or omit `reviewers` "
                "entirely to inherit from a lower layer"
            )
        return self


# ---- Lessons-loop YAML config (per-pipeline-lessons-yaml PRD, Sprint 1) ----
# Pure config-layer surface for the auto-learning lessons loop (see
# `Settings.enable_lesson_distillation`/`.enable_semantic_lesson_retrieval`/
# `.lesson_distill_runner`/`.lesson_distill_model`/`.lesson_min_score`/
# `.lesson_inject_limit` in hivepilot/config.py -- the global floor that
# `lessons_service.distill_lessons`/`retrieve_lessons` currently read
# directly). This sprint only adds the pipeline-level YAML override surface +
# the resolver that reconciles it with the global floor -- no consumption-
# site wiring yet, so a `lessons:` block in pipelines.yaml is inert until a
# later sprint threads `resolve_lessons_config` into those call sites.
#
# SECURITY: this is the "empty-value-fail-open" bug class HivePilot has hit
# before (see error-registry). A `lessons:` block must never be able to turn
# a global fail-closed gate OFF: the two enable flags are OR'd (strengthen-
# only, a pipeline `False`/absent can never override a floor `True`), and
# `min_score`/`inject_limit` must be validated at load time so a bad value
# (0, negative, >1, NaN, inf for min_score; <1 for inject_limit) is rejected
# before it can ever reach the distillation/retrieval gate.
class LessonsConfig(BaseModel):
    enable_distillation: bool | None = None
    enable_semantic: bool | None = None
    distill_runner: str | None = None
    distill_model: str | None = None
    min_score: float | None = None
    inject_limit: int | None = None

    @field_validator("min_score")
    @classmethod
    def _validate_min_score(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not math.isfinite(v) or not (0 < v <= 1):
            raise ValueError(f"lessons.min_score must be a finite number in (0, 1], got {v!r}")
        return v

    @field_validator("inject_limit")
    @classmethod
    def _validate_inject_limit(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 1:
            raise ValueError(f"lessons.inject_limit must be >= 1, got {v!r}")
        return v

    @field_validator("distill_runner", "distill_model")
    @classmethod
    def _reject_blank_override(cls, v: str | None, info: ValidationInfo) -> str | None:
        # A present-but-blank runner/model ("" or whitespace-only) is falsy and
        # would silently fall through to the floor value in
        # `resolve_lessons_config` -- indistinguishable from "unset", which
        # hides a config typo. Reject it at load so a present value is always
        # meaningful (mirrors `DebateConfig._reject_blank_override`). Omit the
        # key / use None to inherit the floor.
        if v is not None and not v.strip():
            raise ValueError(
                f"lessons.{info.field_name} must be a non-empty string when set "
                "(omit the key to inherit the pipeline/floor value)"
            )
        return v


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
    # Stage scoping (only_modules): restrict this stage's NON-GROUP targets to
    # specific modules within each target project's `modules` map (monorepo-
    # modules PRD). None (the default) is byte-identical to before this field
    # existed -- every existing pipeline is completely unaffected. When set to
    # a non-empty list, `Orchestrator._expand_stage_targets_for_modules`
    # expands each plain project target into one `<project>/<module>` target
    # per named module (see `hivepilot.services.project_service.
    # resolve_project_target` for how that string resolves to a module-scoped
    # working directory) -- ONLY in the non-group / single-project targets
    # path; group-mode fan-out (`only_components`/`only_tags`) is untouched.
    # `Orchestrator._validate_stage_modules` fails closed, up front (before
    # any stage executes), if a named module is undefined for a target
    # project or the target project has no `modules` map at all.
    #
    # FAIL-CLOSED: an explicit `only_modules: []` is REJECTED here at load
    # time (see `_validate_only_modules` below) rather than given run-time
    # meaning. This is the same "empty-value-fail-open" bug class this repo
    # has hit before (see error-registry: security-gate-empty-value-fail-
    # open) -- an empty scoping gate must never be interpreted as "run
    # unscoped" (which would fan a module-only stage across the whole
    # project) nor silently as "skip" (a behaviour change invisible in a
    # diff). Making `[]` unrepresentable removes the ambiguity entirely;
    # omit the field (None) to run unscoped instead.
    only_modules: list[str] | None = None
    # When True, a failed stage does not fail-fast the run (the pipeline
    # continues to the next stage instead of breaking).
    continue_on_failure: bool = False
    # Declared-scope gate (SURFACES restriction follow-up): when True, a
    # `SURFACES:` line in THIS stage's agent output sets the run's declared
    # touched-surfaces scope (see `only_modules` / `_parse_surfaces` in
    # `hivepilot.orchestrator`). Default False -- byte-identical to before
    # this field existed: with no stage flagged, `selected_modules` stays
    # `None` for the whole run (fail-safe: every `only_modules` stage runs
    # unscoped). Only the designated planning stage (e.g. the CTO stage)
    # should set this to True; every other stage's output is never scanned
    # for a `SURFACES:` line, no matter what it contains.
    declares_surfaces: bool = False
    # Skill plugin type (skill-plugin-type PRD, Sprint 3): ordered, deduped
    # names of plugin-contributed skills this stage wants applied. Default
    # None -- dormant, byte-identical when absent. See `TaskStep.skills` for
    # the identical semantics and `hivepilot/services/config_validation.py`
    # for the fail-closed cross-reference + `min_role` check.
    skills: list[str] | None = None
    # Stage-level debate/consensus override (debate-judge-pipeline-yaml PRD,
    # Sprint 1). None -- dormant, byte-identical when absent. See
    # `resolve_debate_config` for the strengthen-only precedence over
    # `PipelineConfig.debate` and the global settings floor.
    debate: DebateConfig | None = None

    @field_validator("skills")
    @classmethod
    def _dedup_skills(cls, v: list[str] | None) -> list[str] | None:
        return _dedup_ordered(v)

    @field_validator("only_modules")
    @classmethod
    def _validate_only_modules(cls, v: list[str] | None) -> list[str] | None:
        # See the `only_modules` field's docstring above for the fail-closed
        # rationale: `[]` is unrepresentable (reject-at-model), and every
        # entry must be a real, non-blank module name.
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError(
                "PipelineStage.only_modules must be omitted (None) to run "
                "unscoped, or a non-empty list of module names -- an explicit "
                "empty list ([]) is rejected at load to avoid the empty-"
                "scoping-gate ambiguity (fail-closed: does [] mean 'skip' or "
                "'run against every module'?)"
            )
        for name in v:
            if not name.strip():
                raise ValueError(
                    "PipelineStage.only_modules must not contain blank/whitespace-only module names"
                )
        return v


class CompositionConfig(BaseModel):
    """Per-pipeline opt-in for Chief-of-Staff team composition.

    ``selector_stage`` names the stage whose output carries the ``TEAM:``
    directive. Without one, nothing ever proposes a team, so ``enabled`` would
    be a lie -- and a lie that reads as "a smaller team was chosen" rather than
    "nobody chose". Composition therefore resolves OFF when it is absent.
    """

    enabled: bool | None = None
    selector_stage: str | None = None


@dataclass(frozen=True)
class EffectiveCompositionConfig:
    enabled: bool
    selector_stage: str | None


def resolve_composition_config(
    *,
    floor: Any = None,
    pipeline: "PipelineConfig | None",
) -> EffectiveCompositionConfig:
    """Resolve team composition. **AND**, not the usual strengthen-only OR.

    Every other per-pipeline block here (``debate:``, ``lessons:``) can only ADD
    gating, so OR-ing the layers is fail-closed and correct. Composition
    inverts that: it REMOVES stages, and "enabled" is the permissive state. An
    OR would let a single layer switch off the CISO for a pipeline, and the
    config repo is writable by the very runs this would govern.

    So the floor is a PERMISSION rather than a default, and both layers must
    say yes: the deployment allows it, and the pipeline asks for it. Either one
    silent, and the full roster runs.
    """
    allowed = bool(getattr(floor, "allow_team_composition", False))
    block = getattr(pipeline, "composition", None) if pipeline is not None else None
    requested = bool(block is not None and block.enabled)
    selector = block.selector_stage if block is not None else None
    return EffectiveCompositionConfig(
        enabled=allowed and requested and bool(selector),
        selector_stage=selector,
    )


class VerificationCheck(BaseModel):
    """One deterministic check the release gate must pass.

    ``command`` is operator-declared configuration, run the same way a declared
    shell step is run -- same trust level, deliberately. Agent output is never
    interpolated into it: the entire value of a check is that no model can
    influence its verdict.
    """

    name: str
    #: A command run in the project's tree, OR `ci: true` to consult the
    #: forge's check runs. Exactly one -- see `verification_service.Check`.
    command: str | None = None
    ci: bool = False
    timeout_seconds: int = 300


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
    # Deterministic checks the release gate must pass, run in the project's
    # working tree after the stages. The one gate input that is not an LLM
    # opinion: measured on the memory A/B, six review outputs totalling ~62 000
    # characters missed a raw ESC byte in the produced tool that a five-line
    # probe caught instantly.
    #
    # Empty (the default) is BYTE-IDENTICAL to pre-existing behaviour -- no
    # check runs and the gate is governed by the agent verdict alone. But an
    # empty list is reported as "nothing was verified", never as a pass.
    checks: list[VerificationCheck] = Field(default_factory=list)
    # Chief-of-Staff team composition. Absent (the default) means the full
    # roster runs, and it takes BOTH this and the deployment floor to enable --
    # see `resolve_composition_config` for why this one is an AND.
    composition: CompositionConfig | None = None
    stages: list[PipelineStage] = Field(default_factory=list)
    # Pipeline-wide debate/consensus override (debate-judge-pipeline-yaml PRD,
    # Sprint 1). None -- dormant, byte-identical when absent. A stage's own
    # `debate` block overrides this per-field; see `resolve_debate_config`.
    debate: DebateConfig | None = None
    # Pipeline-wide lessons-loop override (per-pipeline-lessons-yaml PRD,
    # Sprint 1). None -- dormant, byte-identical when absent. See
    # `resolve_lessons_config`.
    lessons: LessonsConfig | None = None
    # Which of the deployment's enabled plugins run for this pipeline. None --
    # every loaded plugin, byte-identical when absent. NARROWS only; see
    # `resolve_enabled_plugins` for why the direction is the opposite of
    # `debate`'s.
    plugins: PluginsConfig | None = None


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


class DebateFloor(Protocol):
    """Structural type for the global debate-config floor.

    Satisfied by `hivepilot.config.Settings` (the real floor at runtime) and
    by any lightweight test double exposing the same five attributes --
    `resolve_debate_config` never imports the real `Settings`/`settings`
    module-level, only as a lazy default (see below), so it stays trivially
    testable without touching global state.
    """

    enable_debate_judge: bool
    enable_challenge_arbiter: bool
    judge_runner: str
    judge_model: str | None
    judge_confidence_threshold: float


@dataclass(frozen=True)
class EffectiveDebateConfig:
    """The fully-resolved debate/consensus config for one stage's run,
    after reconciling the global settings floor with any pipeline- and
    stage-level `debate:` overrides via `resolve_debate_config`."""

    enable_judge: bool
    enable_arbiter: bool
    runner: str
    model: str | None
    confidence_threshold: float
    # Adversarial review facet (Sprint 1). Defaulted so every pre-existing
    # call site / test that constructs an `EffectiveDebateConfig` without
    # naming these two fields keeps working byte-identically.
    reviewers: list[str] = field(default_factory=list)
    review_target: str | None = None


class PluginsConfig(BaseModel):
    """Which of the deployment's enabled plugins this pipeline wants.

    `enable` NARROWS, it never widens. A name the deployment did not enable
    grants nothing -- config-repo YAML must not be able to switch on a plugin
    the operator withheld, which would route around the capability policy and
    the tool whitelist.

    The direction is deliberately opposite to `DebateConfig`, which is
    strengthen-only, and the asymmetry is the point: for a GATE, safe means
    you can only add; for a CAPABILITY, safe means you can only remove.
    """

    enable: list[str] | None = None


def resolve_enabled_plugins(*, loaded: set[str], pipeline: PipelineConfig | None) -> set[str]:
    """Plugins that may run for *pipeline*, given what the deployment loaded.

    Absent block, or no pipeline at all (a bare `run`, a review probe), means
    the full loaded set -- byte-identical to before this existed.

    An EMPTY list is not absence. `plugins: {enable: []}` is an operator
    saying "none for this pipeline", and reading it as "no opinion" would
    silently do the opposite of what was written.
    """
    if pipeline is None or pipeline.plugins is None or pipeline.plugins.enable is None:
        return set(loaded)
    return {name for name in pipeline.plugins.enable if name in loaded}


def unscoped_plugin_names(enable: list[str], *, hook_plugins: set[str]) -> list[str]:
    """Names in *enable* that contribute no lifecycle hook, in written order.

    `enable:` scopes `before_step`/`after_step` and nothing else. A runner is
    chosen explicitly by a task (`runner: rtk`), so it never fires unbidden
    and needs no pipeline-level opt-out -- which means `enable: [rtk]` is
    inert, and an operator who wrote it plainly believed otherwise.

    Reported rather than rejected: the line is harmless, and refusing to load
    a pipeline over it would be worse than the misunderstanding. But leaving
    it silent would let a line that looks load-bearing hold nothing up.
    """
    return [name for name in enable if name not in hook_plugins]


def resolve_debate_config(
    *,
    floor: DebateFloor | None = None,
    pipeline: PipelineConfig | None,
    stage: PipelineStage | None,
) -> EffectiveDebateConfig:
    """Resolve the effective debate/consensus config for *stage* within
    *pipeline*, reconciling the global settings floor with any YAML-level
    `debate:` overrides.

    Precedence (HYBRIDE, see debate-judge-pipeline-yaml PRD):

    - ``enable_judge`` / ``enable_arbiter``: OR across floor + pipeline.debate
      + stage.debate -- STRENGTHEN-ONLY. A pipeline/stage `False` or absent
      value can never turn OFF a floor `True`; only an explicit `True` at any
      layer can turn a floor `False` ON. This is the fail-closed invariant:
      a `debate:` block can only add gating, never remove it.
    - ``runner`` / ``model``: ``stage.debate`` overrides ``pipeline.debate``
      overrides the floor (``judge_runner``/``judge_model``), first non-None
      wins -- same "stage overrides pipeline overrides floor" shape as
      `resolve_stage_model`/`resolve_effort`.
    - ``confidence_threshold``: same override chain as runner/model, but
      every present value was already validated to `(0, 1]` at YAML-load
      time by `DebateConfig`'s field validator, so the resolved value here is
      ALWAYS finite and `> 0` -- absence never degrades to `0`/`None`-as-allow.
    - ``reviewers``: first non-None list wins -- ``stage.debate.reviewers``
      overrides ``pipeline.debate.reviewers`` overrides ``[]`` (no
      reviewers). There is deliberately NO `DebateFloor`/`Settings` floor for
      review (see the module comment above `DebateConfig`): review is a pure
      per-pipeline/per-stage opt-in facet, not a global gate like the judge/
      arbiter enable flags.
    - ``review_target``: same "stage overrides pipeline" first-non-None-wins
      chain, no floor, ``None`` (no review requested) when unset anywhere.

    FAIL-CLOSED (reviewers/review_target): a resolved ``review_target`` with
    an empty resolved ``reviewers`` list raises `ValueError`. `DebateConfig`'s
    model validator already rejects this within a single YAML block; this
    resolve-time check is the backstop for the cross-block case (e.g.
    pipeline sets `review_target`, stage independently zeroes out
    `reviewers`) that the per-block validator cannot see.

    ``floor`` defaults to the live `hivepilot.config.settings` singleton
    (imported lazily, only when the caller doesn't supply one, to keep this
    module import-time independent of `hivepilot.config`); pass an explicit
    `floor` (e.g. a test double) for testability -- when one is passed, no
    import of the real singleton ever happens.
    """
    if floor is None:
        from hivepilot.config import settings as floor  # noqa: PLC0415

    pipeline_debate = pipeline.debate if pipeline is not None else None
    stage_debate = stage.debate if stage is not None else None

    enable_judge = (
        bool(floor.enable_debate_judge)
        or bool(pipeline_debate and pipeline_debate.enable_judge)
        or bool(stage_debate and stage_debate.enable_judge)
    )
    enable_arbiter = (
        bool(floor.enable_challenge_arbiter)
        or bool(pipeline_debate and pipeline_debate.enable_arbiter)
        or bool(stage_debate and stage_debate.enable_arbiter)
    )
    runner = (
        (stage_debate and stage_debate.runner)
        or (pipeline_debate and pipeline_debate.runner)
        or floor.judge_runner
    )
    model = (
        (stage_debate and stage_debate.model)
        or (pipeline_debate and pipeline_debate.model)
        or floor.judge_model
    )
    confidence_threshold = (
        (stage_debate and stage_debate.confidence_threshold)
        or (pipeline_debate and pipeline_debate.confidence_threshold)
        or floor.judge_confidence_threshold
    )
    reviewers = (
        stage_debate.reviewers
        if stage_debate is not None and stage_debate.reviewers is not None
        else (
            pipeline_debate.reviewers
            if pipeline_debate is not None and pipeline_debate.reviewers is not None
            else []
        )
    )
    review_target = (
        stage_debate.review_target
        if stage_debate is not None and stage_debate.review_target is not None
        else (
            pipeline_debate.review_target
            if pipeline_debate is not None and pipeline_debate.review_target is not None
            else None
        )
    )

    if review_target is not None and not reviewers:
        # Fail-closed backstop: a review was requested (review_target set)
        # but no reviewer ever got attached anywhere in the resolved chain.
        # `DebateConfig`'s model validator already blocks this within a
        # single block; this catches the cross-block case it structurally
        # cannot see (see the module comment above `DebateConfig`).
        raise ValueError(
            f"resolved review_target={review_target!r} but the resolved reviewers list "
            "is empty (fail-closed): a review_target requires at least one reviewer at "
            "the pipeline or stage level. This can happen even when each individual "
            "`debate:` block is independently valid (e.g. pipeline sets review_target, "
            "stage independently sets reviewers=[]) -- set `reviewers` wherever "
            "`review_target` is set, or at a layer beneath it."
        )

    return EffectiveDebateConfig(
        enable_judge=enable_judge,
        enable_arbiter=enable_arbiter,
        runner=runner,
        model=model,
        confidence_threshold=confidence_threshold,
        reviewers=reviewers,
        review_target=review_target,
    )


class LessonsFloor(Protocol):
    """Structural type for the global lessons-loop config floor.

    Satisfied by `hivepilot.config.Settings` (the real floor at runtime) and
    by any lightweight test double exposing the same six attributes --
    `resolve_lessons_config` never imports the real `Settings`/`settings`
    module-level, only as a lazy default (see below), so it stays trivially
    testable without touching global state.
    """

    enable_lesson_distillation: bool
    enable_semantic_lesson_retrieval: bool
    lesson_distill_runner: str
    lesson_distill_model: str | None
    lesson_min_score: float
    lesson_inject_limit: int


@dataclass(frozen=True)
class EffectiveLessonsConfig:
    """The fully-resolved lessons-loop config for one pipeline's run, after
    reconciling the global settings floor with any pipeline-level `lessons:`
    override via `resolve_lessons_config`."""

    enable_distillation: bool
    enable_semantic: bool
    distill_runner: str
    distill_model: str | None
    min_score: float
    inject_limit: int


def resolve_lessons_config(
    *,
    floor: LessonsFloor | None = None,
    pipeline: PipelineConfig | None,
) -> EffectiveLessonsConfig:
    """Resolve the effective lessons-loop config for *pipeline*, reconciling
    the global settings floor with any YAML-level `lessons:` override.

    Precedence (HYBRIDE, see per-pipeline-lessons-yaml PRD):

    - ``enable_distillation`` / ``enable_semantic``: OR across floor +
      pipeline.lessons -- STRENGTHEN-ONLY. A pipeline `False` or absent value
      can never turn OFF a floor `True`; only an explicit `True` at either
      layer can turn a floor `False` ON. This is the fail-closed invariant: a
      `lessons:` block can only add gating, never remove it.
    - ``distill_runner`` / ``distill_model`` / ``min_score`` / ``inject_limit``:
      ``pipeline.lessons`` overrides the floor (``lesson_distill_runner``/
      ``lesson_distill_model``/``lesson_min_score``/``lesson_inject_limit``),
      first non-None wins -- same "pipeline overrides floor" shape as
      `resolve_stage_model`/`resolve_effort`'s pipeline-vs-stage layer, minus
      the stage tier (no `PipelineStage.lessons` exists).
    - ``min_score`` / ``inject_limit`` were already validated at YAML-load
      time by `LessonsConfig`'s field validators (`min_score` finite and in
      `(0, 1]`, `inject_limit >= 1`), so the resolved values here can never
      degrade to a `0`/negative sentinel that would silently allow-all or
      disable the lesson-quality/injection floor.

    ``floor`` defaults to the live `hivepilot.config.settings` singleton
    (imported lazily, only when the caller doesn't supply one, to keep this
    module import-time independent of `hivepilot.config`); pass an explicit
    `floor` (e.g. a test double) for testability -- when one is passed, no
    import of the real singleton ever happens.
    """
    if floor is None:
        from hivepilot.config import settings as floor  # noqa: PLC0415

    pipeline_lessons = pipeline.lessons if pipeline is not None else None

    enable_distillation = bool(floor.enable_lesson_distillation) or bool(
        pipeline_lessons and pipeline_lessons.enable_distillation
    )
    enable_semantic = bool(floor.enable_semantic_lesson_retrieval) or bool(
        pipeline_lessons and pipeline_lessons.enable_semantic
    )
    distill_runner = (
        pipeline_lessons and pipeline_lessons.distill_runner
    ) or floor.lesson_distill_runner
    distill_model = (
        pipeline_lessons and pipeline_lessons.distill_model
    ) or floor.lesson_distill_model
    min_score = (pipeline_lessons and pipeline_lessons.min_score) or floor.lesson_min_score
    inject_limit = (pipeline_lessons and pipeline_lessons.inject_limit) or floor.lesson_inject_limit

    return EffectiveLessonsConfig(
        enable_distillation=enable_distillation,
        enable_semantic=enable_semantic,
        distill_runner=distill_runner,
        distill_model=distill_model,
        min_score=min_score,
        inject_limit=inject_limit,
    )


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
