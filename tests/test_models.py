"""Tests for hivepilot.models — runner definition schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hivepilot.models import Group, PipelineStage, RunnerDefinition, RunnerKind


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


def test_runner_definition_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        RunnerDefinition(kind="does-not-exist")  # type: ignore[arg-type]


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
