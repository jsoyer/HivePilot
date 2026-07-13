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
# PRD A1 Sprint 1 — stage scoping + continue_on_failure defaults
# ---------------------------------------------------------------------------


class TestPipelineStageScopingDefaults:
    """New PipelineStage fields default to backward-compatible values."""

    def test_only_components_defaults_to_none(self) -> None:
        stage = PipelineStage(name="x", task="t")
        assert stage.only_components is None

    def test_only_tags_defaults_to_none(self) -> None:
        stage = PipelineStage(name="x", task="t")
        assert stage.only_tags is None

    def test_continue_on_failure_defaults_to_false(self) -> None:
        stage = PipelineStage(name="x", task="t")
        assert stage.continue_on_failure is False

    def test_fields_accept_explicit_values(self) -> None:
        stage = PipelineStage(
            name="x",
            task="t",
            only_components=["c1", "c2"],
            only_tags=["frontend"],
            continue_on_failure=True,
        )
        assert stage.only_components == ["c1", "c2"]
        assert stage.only_tags == ["frontend"]
        assert stage.continue_on_failure is True


class TestGroupTagsDefault:
    """Group.tags defaults to an empty dict (tag -> component names)."""

    def test_tags_defaults_to_empty_dict(self) -> None:
        group = Group(description="d", hub="h", components=[])
        assert group.tags == {}

    def test_tags_accepts_explicit_mapping(self) -> None:
        group = Group(
            description="d",
            hub="h",
            components=["c1", "c2"],
            tags={"frontend": ["c1"], "backend": ["c2"]},
        )
        assert group.tags == {"frontend": ["c1"], "backend": ["c2"]}
