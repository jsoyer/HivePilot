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
# PRD A1 / Sprint 1 — stage scoping fields
# ---------------------------------------------------------------------------


def test_pipeline_stage_scoping_fields_default() -> None:
    """New PipelineStage fields are additive and default to None/None/False."""
    stage = PipelineStage(name="x", task="t")
    assert stage.only_components is None
    assert stage.only_tags is None
    assert stage.continue_on_failure is False


def test_pipeline_stage_scoping_fields_accept_values() -> None:
    stage = PipelineStage(
        name="x",
        task="t",
        only_components=["acme-api"],
        only_tags=["backend"],
        continue_on_failure=True,
    )
    assert stage.only_components == ["acme-api"]
    assert stage.only_tags == ["backend"]
    assert stage.continue_on_failure is True


def test_group_tags_defaults_to_empty_dict() -> None:
    """Group.tags is additive and defaults to an empty dict."""
    group = Group(description="d", hub="h", components=[])
    assert group.tags == {}


def test_group_tags_accepts_mapping() -> None:
    group = Group(
        description="d",
        hub="h",
        components=["acme-api", "acme-web"],
        tags={"backend": ["acme-api"]},
    )
    assert group.tags == {"backend": ["acme-api"]}
