"""Tests for hivepilot.models — runner definition schema."""

from __future__ import annotations

import pydantic
import pytest

from hivepilot.models import (
    KNOWN_RUNNER_KINDS,
    Group,
    PipelineStage,
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
        RunnerDefinition(kind="claude", effort="bogus")


def test_runner_definition_effort_defaults_to_none() -> None:
    definition = RunnerDefinition(kind="claude")
    assert definition.effort is None


def test_task_step_effort_accepts_valid_level() -> None:
    step = TaskStep(name="x", runner="claude", effort="max")
    assert step.effort == "max"


def test_task_step_effort_rejects_invalid_level() -> None:
    with pytest.raises(pydantic.ValidationError):
        TaskStep(name="x", runner="claude", effort="bogus")


def test_task_step_effort_defaults_to_none() -> None:
    step = TaskStep(name="x", runner="claude")
    assert step.effort is None
