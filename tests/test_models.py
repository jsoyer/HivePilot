"""Tests for hivepilot.models — runner definition schema."""

from __future__ import annotations

import pydantic
import pytest

from hivepilot.models import (
    KNOWN_RUNNER_KINDS,
    Group,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    RunnerKind,
    TaskStep,
)


def test_known_runner_kinds_all_have_runner_map_entries() -> None:
    """Every kind advertised in KNOWN_RUNNER_KINDS must be a real,
    registered runner — no advertised-but-unregistered orphans (the "api"
    bug fixed in roadmap Phase 26a). Prevents this class of regression."""
    from hivepilot.registry import RUNNER_MAP

    for kind in KNOWN_RUNNER_KINDS:
        assert kind in RUNNER_MAP, (
            f"{kind!r} is advertised in KNOWN_RUNNER_KINDS but has no RUNNER_MAP entry"
        )


def test_api_is_no_longer_a_known_runner_kind() -> None:
    """The "api" runner kind was a pure orphan (name only, never backed by a
    runner class) — it must not be re-advertised as valid."""
    assert "api" not in KNOWN_RUNNER_KINDS


def test_runner_definition_accepts_cursor_kind() -> None:
    """Sprint 2.1: the `cursor` runner kind is a valid RunnerDefinition.kind."""
    definition = RunnerDefinition(name="cursor", kind="cursor")
    assert definition.kind == "cursor"


@pytest.mark.parametrize(
    "kind",
    ["claude", "codex", "gemini", "opencode", "cursor", "container"],
)
def test_runner_definition_known_kinds(kind: RunnerKind) -> None:
    assert RunnerDefinition(kind=kind).kind == kind


def test_runner_definition_accepts_unknown_kind_but_registry_rejects_it() -> None:
    # Intentional PRD-driven contract change (Plugin System PRD, Sprint 1):
    # RunnerKind widened from Literal[...] to str, so pydantic no longer
    # rejects unknown kind strings at construction time; rejection now
    # happens at resolution time, in the registry.
    definition = RunnerDefinition(kind="does-not-exist")
    assert definition.kind == "does-not-exist"

    from hivepilot.registry import RunnerRegistry

    with pytest.raises(KeyError):
        RunnerRegistry({}).get_runner("does-not-exist")


def test_registry_execute_definition_rejects_orphan_api_kind_with_clear_error() -> None:
    """Roadmap Phase 26a: a resolved `RunnerDefinition(kind="api")` — the
    shape produced when a role/task is wired to the historical `"api"`
    orphan kind and routed through `execute_definition`/`capture_definition`
    (the real-world path via `resolve_runner()` -> `RunnerDefinition` ->
    registry) — used to raise a bare `KeyError`. It must now raise a clear,
    descriptive error naming the unknown kind and listing the currently
    available kinds."""
    from unittest.mock import MagicMock

    from hivepilot.registry import RUNNER_MAP, RunnerRegistry

    definition = RunnerDefinition(kind="api")

    with pytest.raises(KeyError) as exc_info:
        RunnerRegistry({}).execute_definition(definition, MagicMock())

    message = str(exc_info.value)
    assert "api" in message
    assert "Unknown runner kind" in message
    for builtin in sorted(RUNNER_MAP):
        assert builtin in message


# ---------------------------------------------------------------------------
# PRD A1 — stage scoping & controls: PipelineStage / Group field defaults
# ---------------------------------------------------------------------------


def test_pipeline_stage_scoping_fields_default_to_none_and_false() -> None:
    """A stage with none of the new fields set behaves exactly as before
    (backward-compatible defaults: only_components=None, only_tags=None,
    continue_on_failure=False)."""
    stage = PipelineStage(name="x", task="t")
    assert stage.only_components is None
    assert stage.only_tags is None
    assert stage.continue_on_failure is False


def test_pipeline_stage_scoping_fields_accept_explicit_values() -> None:
    stage = PipelineStage(
        name="x",
        task="t",
        only_components=["c1"],
        only_tags=["frontend"],
        continue_on_failure=True,
    )
    assert stage.only_components == ["c1"]
    assert stage.only_tags == ["frontend"]
    assert stage.continue_on_failure is True


def test_pipeline_stage_only_modules_defaults_to_none() -> None:
    """A stage with `only_modules` unset behaves exactly as before this field
    existed -- byte-identical default (None)."""
    stage = PipelineStage(name="x", task="t")
    assert stage.only_modules is None


def test_pipeline_stage_only_modules_accepts_non_empty_list() -> None:
    stage = PipelineStage(name="x", task="t", only_modules=["console", "api"])
    assert stage.only_modules == ["console", "api"]


def test_pipeline_stage_only_modules_rejects_empty_list() -> None:
    """An explicit `only_modules: []` is rejected at load (fail-closed choice:
    reject-at-model rather than silently treating [] as skip-the-stage or
    run-unscoped -- see the field's docstring in hivepilot/models.py)."""
    with pytest.raises(pydantic.ValidationError, match="only_modules"):
        PipelineStage(name="x", task="t", only_modules=[])


@pytest.mark.parametrize("bad_modules", [[""], ["  "], ["console", ""], ["console", "   "]])
def test_pipeline_stage_only_modules_rejects_blank_entries(bad_modules: list[str]) -> None:
    with pytest.raises(pydantic.ValidationError, match="only_modules"):
        PipelineStage(name="x", task="t", only_modules=bad_modules)


def test_pipeline_stage_declares_surfaces_defaults_to_false() -> None:
    """A stage with `declares_surfaces` unset behaves exactly as before this
    field existed -- byte-identical default (False): its output is never
    scanned for a `SURFACES:` line."""
    stage = PipelineStage(name="x", task="t")
    assert stage.declares_surfaces is False


def test_pipeline_stage_declares_surfaces_accepts_true() -> None:
    stage = PipelineStage(name="x", task="t", declares_surfaces=True)
    assert stage.declares_surfaces is True


def test_group_tags_defaults_to_empty_dict() -> None:
    group = Group(description="d", hub="h", components=[])
    assert group.tags == {}


def test_group_tags_accepts_tag_to_components_mapping() -> None:
    group = Group(
        description="d",
        hub="h",
        components=["c1", "c2"],
        tags={"frontend": ["c1"], "backend": ["c2"]},
    )
    assert group.tags == {"frontend": ["c1"], "backend": ["c2"]}


def test_group_single_repo_defaults_to_false() -> None:
    """Default single_repo=False keeps every existing multi-repo group
    byte-identical -- this is the opt-in gate for monorepo groups."""
    group = Group(description="d", hub="h", components=["c1", "c2"])
    assert group.single_repo is False


def test_group_single_repo_true_with_hub_is_accepted() -> None:
    group = Group(description="d", hub="hub", single_repo=True, components=["ui", "api"])
    assert group.single_repo is True
    assert group.hub == "hub"


def test_group_single_repo_true_without_hub_raises() -> None:
    """single_repo=True requires a non-empty hub — a monorepo group with no
    hub has nowhere to run its stages."""
    with pytest.raises(ValueError, match="single_repo"):
        Group(description="d", single_repo=True, components=["ui", "api"])


def test_group_single_repo_true_with_empty_string_hub_raises() -> None:
    with pytest.raises(ValueError, match="single_repo"):
        Group(description="d", hub="", single_repo=True, components=["ui", "api"])


# ---------------------------------------------------------------------------
# Phase 17a-B — TaskStep.require_approval (step-level destructive-operation
# approval gate)
# ---------------------------------------------------------------------------


def test_task_step_require_approval_defaults_false() -> None:
    """Backward-compatible default: a step with no explicit flag never gates
    on its own (a destructive runner can still gate it independently)."""
    step = TaskStep(name="s", runner="terraform")
    assert step.require_approval is False


def test_task_step_require_approval_accepts_true() -> None:
    step = TaskStep(name="s", runner="shell", require_approval=True)
    assert step.require_approval is True


# ---------------------------------------------------------------------------
# skill-plugin-type PRD, Sprint 3 — TaskStep.skills / PipelineStage.skills
# ---------------------------------------------------------------------------


def test_task_step_skills_defaults_to_none() -> None:
    """Absence of `skills` must be byte-identical to pre-Sprint-3 behavior:
    default is None, not an empty list."""
    step = TaskStep(name="s", runner="claude")
    assert step.skills is None


def test_task_step_skills_preserves_order_and_dedups() -> None:
    step = TaskStep(name="s", runner="claude", skills=["b", "a", "b", "c", "a"])
    assert step.skills == ["b", "a", "c"]


def test_pipeline_stage_skills_defaults_to_none() -> None:
    stage = PipelineStage(name="x", task="t")
    assert stage.skills is None


def test_pipeline_stage_skills_preserves_order_and_dedups() -> None:
    stage = PipelineStage(name="x", task="t", skills=["z", "y", "z"])
    assert stage.skills == ["z", "y"]


# ---------------------------------------------------------------------------
# Reasoning-effort knob — RunnerDefinition.effort / TaskStep.effort
# (Claude-runner MAX_THINKING_TOKENS translation lives in
# hivepilot.runners.claude_runner; this module only validates the level string)
# ---------------------------------------------------------------------------


def test_runner_definition_effort_accepts_valid_level() -> None:
    definition = RunnerDefinition(kind="claude", effort="high")
    assert definition.effort == "high"


def test_runner_definition_effort_rejects_invalid_level() -> None:
    with pytest.raises(pydantic.ValidationError):
        RunnerDefinition(kind="claude", effort="bogus")  # type: ignore[arg-type]


def test_runner_definition_effort_defaults_to_none() -> None:
    definition = RunnerDefinition(kind="claude")
    assert definition.effort is None


def test_task_step_effort_accepts_valid_level() -> None:
    step = TaskStep(name="x", runner="claude", effort="max")
    assert step.effort == "max"


def test_task_step_effort_rejects_invalid_level() -> None:
    with pytest.raises(pydantic.ValidationError):
        TaskStep(name="x", runner="claude", effort="bogus")  # type: ignore[arg-type]


def test_task_step_effort_defaults_to_none() -> None:
    step = TaskStep(name="x", runner="claude")
    assert step.effort is None


# ---------------------------------------------------------------------------
# ProjectConfig.modules / ui_surfaces — first-class monorepo module support
# (monorepo-modules PRD). Additive fields: default empty everywhere -> zero
# behaviour change for a project that doesn't declare `modules`.
# ---------------------------------------------------------------------------


def test_project_config_modules_defaults_to_empty(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)
    assert project.modules == {}
    assert project.ui_surfaces == []


def test_project_config_accepts_modules_map(tmp_path) -> None:
    (tmp_path / "apps" / "detection-core").mkdir(parents=True)
    (tmp_path / "apps" / "workers" / "adjudication").mkdir(parents=True)
    project = ProjectConfig(
        path=tmp_path,
        modules={
            "detection-core": "apps/detection-core",
            "adjudication": "apps/workers/adjudication",
        },
        ui_surfaces=["detection-core"],
    )
    assert project.modules == {
        "detection-core": "apps/detection-core",
        "adjudication": "apps/workers/adjudication",
    }
    assert project.ui_surfaces == ["detection-core"]


def test_project_config_rejects_dotdot_traversal_module_subpath(tmp_path) -> None:
    """SECURITY: a module subpath that escapes `project.path` via `..` must
    never be accepted — otherwise a malicious/typo'd `modules:` entry could
    let an agent operate outside the repo checkout entirely."""
    with pytest.raises(pydantic.ValidationError, match="escapes|outside|traversal"):
        ProjectConfig(path=tmp_path, modules={"evil": "../../etc"})


def test_project_config_rejects_absolute_module_subpath(tmp_path) -> None:
    """SECURITY: an absolute subpath must never be accepted either — it would
    silently ignore `project.path` entirely and scope the agent to an
    attacker/typo-controlled absolute filesystem location."""
    with pytest.raises(pydantic.ValidationError, match="absolute|outside"):
        ProjectConfig(path=tmp_path, modules={"evil": "/etc/passwd"})


def test_project_config_module_subpath_must_resolve_inside_project_path(tmp_path) -> None:
    """A subpath like `foo/../../bar` that arithmetically stays inside via
    naive string checks but `.resolve()`s to outside `project.path` must
    also be rejected — the check MUST be on the resolved path, not the raw
    string (a bare `..` substring check is insufficient and easy to bypass)."""
    sibling = tmp_path.parent / "sibling-escape"
    rel = f"../{sibling.name}"
    with pytest.raises(pydantic.ValidationError):
        ProjectConfig(path=tmp_path, modules={"evil": rel})


# ---------------------------------------------------------------------------
# ProjectConfig.forge — forge plugin type (Phase 1: GitHub default,
# fail-closed on an unknown forge name). See hivepilot/forges/.
# ---------------------------------------------------------------------------


def test_project_config_forge_defaults_to_github(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)
    assert project.forge == "github"
    assert project.forge_base_url is None


def test_project_config_rejects_unknown_forge(tmp_path) -> None:
    """FAIL-CLOSED: an unknown `forge` value must raise at config-load time --
    never silently fall back to GitHub (mirrors the unknown-runner-kind
    posture, just enforced earlier, at model validation)."""
    with pytest.raises(pydantic.ValidationError, match="[Uu]nknown forge"):
        ProjectConfig(path=tmp_path, forge="bitbucket")


def test_project_config_accepts_registered_forge(tmp_path) -> None:
    """A forge name that IS registered in FORGE_MAP must be accepted --
    proves the validator checks the live registry, not a hardcoded
    allowlist."""
    from hivepilot.forges.provider import FORGE_MAP, ForgeRegistry

    class _FakeForge:
        name = "fake-vcs"

    ForgeRegistry.register("fake-vcs", _FakeForge(), override=True)  # type: ignore[arg-type]
    try:
        project = ProjectConfig(path=tmp_path, forge="fake-vcs")
        assert project.forge == "fake-vcs"
    finally:
        FORGE_MAP.pop("fake-vcs", None)


# ---------------------------------------------------------------------------
# ProjectConfig.instruction_files — inline-repo-instructions PRD. Generic
# list of repository instruction/context files (unlike `claude_md`, not tied
# to any one vendor's convention). Additive: defaults to None, `claude_md`
# keeps meaning exactly what it always did.
# ---------------------------------------------------------------------------


def test_project_config_instruction_files_defaults_to_none(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)
    assert project.instruction_files is None
    assert project.claude_md is None


def test_project_config_accepts_instruction_files_list(tmp_path) -> None:
    project = ProjectConfig(
        path=tmp_path,
        instruction_files=["AGENTS.md", "AGENT-GOVERNANCE.md", ".cursorrules"],
    )
    assert project.instruction_files == ["AGENTS.md", "AGENT-GOVERNANCE.md", ".cursorrules"]


def test_project_config_dedups_instruction_files_preserving_order(tmp_path) -> None:
    """Mirrors the existing `TaskStep.skills` dedup behavior (`_dedup_ordered`)
    — a duplicate entry collapses to one, first occurrence wins."""
    project = ProjectConfig(
        path=tmp_path,
        instruction_files=["AGENTS.md", "RULES.md", "AGENTS.md"],
    )
    assert project.instruction_files == ["AGENTS.md", "RULES.md"]


def test_project_config_claude_md_unaffected_by_instruction_files(tmp_path) -> None:
    """`claude_md` keeps meaning exactly what it did before this field
    existed — both can be set independently, neither overrides the other."""
    project = ProjectConfig(path=tmp_path, claude_md="CLAUDE.md", instruction_files=["AGENTS.md"])
    assert project.claude_md == "CLAUDE.md"
    assert project.instruction_files == ["AGENTS.md"]
