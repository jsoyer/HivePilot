"""E3 — agents pick the impacted component subset via a COMPONENTS: line."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
from hivepilot.orchestrator import (
    _parse_components,
    _resolve_stage_target_components,
    _stage_should_skip,
    _validate_stage_tags,
)

# ---------------------------------------------------------------------------
# PRD A1 — stage scoping: pure helper functions
# ---------------------------------------------------------------------------


class TestResolveStageTargetComponents:
    def test_only_components_alone(self) -> None:
        stage = PipelineStage(name="s", task="t", only_components=["c1", "c2"])
        assert _resolve_stage_target_components(stage, {}) == {"c1", "c2"}

    def test_only_tags_match_resolves_via_group_tags(self) -> None:
        stage = PipelineStage(name="s", task="t", only_tags=["frontend"])
        group_tags = {"frontend": ["web", "ui"]}
        assert _resolve_stage_target_components(stage, group_tags) == {"web", "ui"}

    def test_union_of_both_only_components_and_only_tags(self) -> None:
        stage = PipelineStage(name="s", task="t", only_components=["c1"], only_tags=["frontend"])
        group_tags = {"frontend": ["web"]}
        assert _resolve_stage_target_components(stage, group_tags) == {"c1", "web"}

    def test_neither_selector_returns_empty_set(self) -> None:
        stage = PipelineStage(name="s", task="t")
        assert _resolve_stage_target_components(stage, {}) == set()


class TestValidateStageTags:
    def test_undefined_tag_raises_value_error_naming_the_tag(self) -> None:
        """Fail-closed: an only_tags value absent from Group.tags must raise,
        naming the offending tag — a review/security stage must never be
        silently bypassed by a typo'd or missing tag."""
        stage = PipelineStage(name="security-review", task="review", only_tags=["security"])
        with pytest.raises(ValueError, match="security"):
            _validate_stage_tags([stage], {})

    def test_defined_tag_does_not_raise(self) -> None:
        stage = PipelineStage(name="s", task="t", only_tags=["frontend"])
        _validate_stage_tags([stage], {"frontend": ["web"]})  # must not raise

    def test_stage_without_only_tags_never_raises(self) -> None:
        stage = PipelineStage(name="s", task="t")
        _validate_stage_tags([stage], {})  # must not raise


class TestStageShouldSkip:
    def test_no_selector_always_runs(self) -> None:
        stage = PipelineStage(name="s", task="t")
        assert _stage_should_skip(stage, {}, ["c1"]) is False

    def test_skip_excludes_stage_when_target_disjoint_from_selected(self) -> None:
        stage = PipelineStage(name="s", task="t", only_components=["c9"])
        assert _stage_should_skip(stage, {}, ["c1", "c2"]) is True

    def test_no_skip_when_only_components_matches_selected(self) -> None:
        stage = PipelineStage(name="s", task="t", only_components=["c1", "c9"])
        assert _stage_should_skip(stage, {}, ["c1", "c2"]) is False

    def test_no_skip_when_only_tags_matches_selected(self) -> None:
        stage = PipelineStage(name="s", task="t", only_tags=["frontend"])
        group_tags = {"frontend": ["web"]}
        assert _stage_should_skip(stage, group_tags, ["web", "api"]) is False

    def test_skip_when_only_tags_does_not_match_selected(self) -> None:
        stage = PipelineStage(name="s", task="t", only_tags=["frontend"])
        group_tags = {"frontend": ["web"]}
        assert _stage_should_skip(stage, group_tags, ["api"]) is True


def test_parse_components_extracts_and_intersects() -> None:
    valid = ["acme-api", "acme-web", "acme-worker"]
    text = "Plan...\nCOMPONENTS: acme-api, acme-worker\nmore"
    assert _parse_components(text, valid) == ["acme-api", "acme-worker"]


def test_parse_components_ignores_unknown_and_dedups() -> None:
    valid = ["acme-api"]
    assert _parse_components("COMPONENTS: acme-api, ghost, acme-api", valid) == ["acme-api"]


def test_parse_components_returns_empty_when_absent() -> None:
    assert _parse_components("no marker here", ["acme-api"]) == []


def _orch(pipeline: PipelineConfig):
    from hivepilot.orchestrator import Orchestrator

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch(
            "hivepilot.orchestrator.load_pipelines",
            return_value=PipelinesFile(pipelines={"p": pipeline}),
        ),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        return Orchestrator()


def test_execution_fans_out_only_to_selected_components() -> None:
    from hivepilot.orchestrator import RunResult

    pipeline = PipelineConfig(
        description="t",
        stages=[
            PipelineStage(name="plan", task="plan"),
            PipelineStage(name="synth", task="synth"),
            PipelineStage(name="build", task="build", pause_before=True),
            PipelineStage(name="ship", task="ship"),
        ],
    )
    orch = _orch(pipeline)
    targets: dict[str, list[str]] = {}

    def fake_run_task(**kw):
        targets[kw["task_name"]] = list(kw["project_names"])
        # the synthesizer announces which components the change touches
        detail = "COMPONENTS: c1, c3" if kw["task_name"] == "synth" else "ok"
        return [RunResult(kw["project_names"][0], kw["task_name"], True, detail)]

    with (
        patch("hivepilot.orchestrator.state_service.record_run_start", return_value=1),
        patch("hivepilot.orchestrator.state_service.complete_run"),
        patch("hivepilot.orchestrator.write_stage_artifact", return_value=None),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
        patch.object(orch, "run_task", side_effect=fake_run_task),
    ):
        orch.run_pipeline(
            project_names=["hub"],
            pipeline_name="p",
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
            simulate=True,  # skip the pause so phase 2 runs in the same call
            hub="hub",
            components=["c1", "c2", "c3"],
        )

    assert targets["plan"] == ["hub"]  # planning on the hub
    assert targets["build"] == ["c1", "c3"]  # narrowed to the selected subset (c2 dropped)
    assert targets["ship"] == ["c1", "c3"]
