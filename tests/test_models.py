"""Tests for hivepilot.models — runner definition schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hivepilot.models import RunnerDefinition


def test_runner_definition_accepts_cursor_kind() -> None:
    """Sprint 2.1: the `cursor` runner kind is a valid RunnerDefinition.kind."""
    definition = RunnerDefinition(name="cursor", kind="cursor")
    assert definition.kind == "cursor"


@pytest.mark.parametrize(
    "kind",
    ["claude", "codex", "gemini", "opencode", "cursor", "container"],
)
def test_runner_definition_known_kinds(kind: str) -> None:
    assert RunnerDefinition(kind=kind).kind == kind


def test_runner_definition_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        RunnerDefinition(kind="does-not-exist")
