"""Tests for the YAML-facing debate/consensus config layer
(debate-judge-pipeline-yaml PRD, Sprint 1).

Covers:
- `DebateConfig` model parsing (absent / full / partial `debate:` blocks).
- `DebateConfig.confidence_threshold`'s field-validator matrix -- the load-time
  guard that a bad value (0, negative, >1, NaN, inf) is rejected before it can
  ever reach the fail-closed PR gate.
- `resolve_debate_config`'s HYBRIDE precedence: strengthen-only OR across
  enable flags (a pipeline/stage `False`/absent can never turn OFF a floor
  `True`), stage-overrides-pipeline-overrides-floor for runner/model/
  threshold, and the invariant that the resolved threshold is always finite
  and > 0.
- `validate_pipeline`'s defense-in-depth re-check of `confidence_threshold` at
  both the pipeline level and every stage level (using `model_construct` to
  simulate a value that slipped past the pydantic validator).

This sprint is a pure config layer -- no orchestrator wiring. A `debate:`
block in pipelines.yaml is inert until a later sprint threads
`resolve_debate_config` into the orchestrator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from hivepilot.models import (
    DebateConfig,
    EffectiveDebateConfig,
    PipelineConfig,
    PipelineStage,
    TasksFile,
    resolve_debate_config,
)
from hivepilot.services.pipeline_service import validate_pipeline


@dataclass
class _FakeFloor:
    """Lightweight test double for the global settings floor -- exposes only
    the five fields `resolve_debate_config` reads. Does NOT depend on the
    real `hivepilot.config.settings` singleton, so these tests are immune to
    whatever the real defaults happen to be."""

    enable_debate_judge: bool = False
    enable_challenge_arbiter: bool = False
    judge_runner: str = "claude"
    judge_model: str | None = None
    judge_confidence_threshold: float = 0.5


def _tasks() -> TasksFile:
    return TasksFile(tasks={})


class TestDebateConfigParsing:
    def test_debate_absent_is_none(self) -> None:
        stage = PipelineStage(name="Stage A", task="t")
        pipeline = PipelineConfig(description="d", stages=[stage])
        assert stage.debate is None
        assert pipeline.debate is None

    def test_full_block_parses(self) -> None:
        cfg = DebateConfig(
            enable_judge=True,
            enable_arbiter=True,
            runner="codex",
            model="gpt-5",
            confidence_threshold=0.75,
        )
        assert cfg.enable_judge is True
        assert cfg.enable_arbiter is True
        assert cfg.runner == "codex"
        assert cfg.model == "gpt-5"
        assert cfg.confidence_threshold == 0.75

    def test_partial_block_parses_rest_none(self) -> None:
        cfg = DebateConfig(enable_judge=True)
        assert cfg.enable_judge is True
        assert cfg.enable_arbiter is None
        assert cfg.runner is None
        assert cfg.model is None
        assert cfg.confidence_threshold is None

    def test_pipeline_and_stage_accept_debate_block(self) -> None:
        stage = PipelineStage(name="Stage A", task="t", debate=DebateConfig(enable_judge=True))
        pipeline = PipelineConfig(
            description="d", stages=[stage], debate=DebateConfig(runner="codex")
        )
        assert pipeline.debate is not None
        assert pipeline.debate.runner == "codex"
        assert pipeline.stages[0].debate is not None
        assert pipeline.stages[0].debate.enable_judge is True


class TestConfidenceThresholdValidatorMatrix:
    @pytest.mark.parametrize("bad_value", [0, -0.1, -1, 1.0001, 2, math.nan, math.inf, -math.inf])
    def test_rejects_out_of_range(self, bad_value: float) -> None:
        with pytest.raises(ValidationError):
            DebateConfig(confidence_threshold=bad_value)

    @pytest.mark.parametrize("good_value", [0.5, 1.0, 0.0001])
    def test_accepts_in_range(self, good_value: float) -> None:
        cfg = DebateConfig(confidence_threshold=good_value)
        assert cfg.confidence_threshold == good_value

    def test_none_is_accepted(self) -> None:
        cfg = DebateConfig(confidence_threshold=None)
        assert cfg.confidence_threshold is None

    def test_absent_is_accepted(self) -> None:
        cfg = DebateConfig()
        assert cfg.confidence_threshold is None


class TestResolveDebateConfigPrecedence:
    def test_floor_only_returns_floor_values(self) -> None:
        floor = _FakeFloor(
            enable_debate_judge=True,
            enable_challenge_arbiter=False,
            judge_runner="claude",
            judge_model="opus",
            judge_confidence_threshold=0.6,
        )
        result = resolve_debate_config(floor=floor, pipeline=None, stage=None)
        assert result == EffectiveDebateConfig(
            enable_judge=True,
            enable_arbiter=False,
            runner="claude",
            model="opus",
            confidence_threshold=0.6,
        )

    def test_pipeline_overrides_runner_model_threshold_over_floor(self) -> None:
        floor = _FakeFloor(judge_runner="claude", judge_model=None, judge_confidence_threshold=0.5)
        pipeline = PipelineConfig(
            description="d",
            debate=DebateConfig(runner="codex", model="gpt-5", confidence_threshold=0.8),
        )
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=None)
        assert result.runner == "codex"
        assert result.model == "gpt-5"
        assert result.confidence_threshold == 0.8

    def test_stage_overrides_pipeline_which_overrides_floor(self) -> None:
        floor = _FakeFloor(
            judge_runner="claude", judge_model="opus", judge_confidence_threshold=0.5
        )
        pipeline = PipelineConfig(
            description="d",
            debate=DebateConfig(runner="codex", model="gpt-5", confidence_threshold=0.8),
        )
        stage = PipelineStage(
            name="s",
            task="t",
            debate=DebateConfig(runner="vibe", model="claude-opus", confidence_threshold=0.95),
        )
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.runner == "vibe"
        assert result.model == "claude-opus"
        assert result.confidence_threshold == 0.95

    def test_enable_judge_or_pipeline_false_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_debate_judge=True)
        pipeline = PipelineConfig(description="d", debate=DebateConfig(enable_judge=False))
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=None)
        assert result.enable_judge is True

    def test_enable_arbiter_or_pipeline_false_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_challenge_arbiter=True)
        pipeline = PipelineConfig(description="d", debate=DebateConfig(enable_arbiter=False))
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=None)
        assert result.enable_arbiter is True

    def test_enable_judge_stage_false_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_debate_judge=True)
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(enable_judge=False))
        result = resolve_debate_config(floor=floor, pipeline=None, stage=stage)
        assert result.enable_judge is True

    def test_enable_arbiter_stage_false_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_challenge_arbiter=True)
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(enable_arbiter=False))
        result = resolve_debate_config(floor=floor, pipeline=None, stage=stage)
        assert result.enable_arbiter is True

    def test_pipeline_enable_judge_turns_on_when_floor_false(self) -> None:
        floor = _FakeFloor(enable_debate_judge=False)
        pipeline = PipelineConfig(description="d", debate=DebateConfig(enable_judge=True))
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=None)
        assert result.enable_judge is True

    def test_stage_enable_arbiter_turns_on_when_floor_and_pipeline_false(self) -> None:
        floor = _FakeFloor(enable_challenge_arbiter=False)
        pipeline = PipelineConfig(description="d", debate=DebateConfig(enable_arbiter=False))
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(enable_arbiter=True))
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.enable_arbiter is True

    def test_threshold_absent_at_pipeline_and_stage_inherits_floor(self) -> None:
        floor = _FakeFloor(judge_confidence_threshold=0.42)
        pipeline = PipelineConfig(description="d", debate=DebateConfig(runner="codex"))
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(model="gpt-5"))
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.confidence_threshold == 0.42

    def test_threshold_present_only_at_stage_wins(self) -> None:
        floor = _FakeFloor(judge_confidence_threshold=0.5)
        pipeline = PipelineConfig(description="d", debate=DebateConfig(confidence_threshold=0.6))
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(confidence_threshold=0.9))
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.confidence_threshold == 0.9

    @pytest.mark.parametrize(
        "pipeline,stage",
        [
            (None, None),
            (PipelineConfig(description="d"), None),
            (PipelineConfig(description="d", debate=DebateConfig(enable_judge=True)), None),
            (
                PipelineConfig(description="d"),
                PipelineStage(name="s", task="t", debate=DebateConfig(confidence_threshold=0.9)),
            ),
        ],
    )
    def test_returned_threshold_always_positive_finite(
        self, pipeline: PipelineConfig | None, stage: PipelineStage | None
    ) -> None:
        floor = _FakeFloor(judge_confidence_threshold=0.5)
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert math.isfinite(result.confidence_threshold)
        assert result.confidence_threshold > 0

    def test_none_debate_on_pipeline_and_stage_is_guarded(self) -> None:
        floor = _FakeFloor(enable_debate_judge=True, judge_runner="claude", judge_model="opus")
        pipeline = PipelineConfig(description="d")  # debate=None
        stage = PipelineStage(name="s", task="t")  # debate=None
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result == EffectiveDebateConfig(
            enable_judge=True,
            enable_arbiter=False,
            runner="claude",
            model="opus",
            confidence_threshold=0.5,
        )

    def test_default_floor_param_uses_real_settings_singleton(self) -> None:
        # No `floor` passed -> lazily imports hivepilot.config.settings. Just
        # prove it doesn't raise and returns a well-formed, positive-finite
        # threshold; we don't assert on the real settings' concrete values
        # since this test must stay immune to their defaults changing.
        result = resolve_debate_config(pipeline=None, stage=None)
        assert isinstance(result, EffectiveDebateConfig)
        assert math.isfinite(result.confidence_threshold)
        assert result.confidence_threshold > 0


class TestValidatePipelineDebateThreshold:
    def test_pipeline_level_bad_threshold_raises(self) -> None:
        bad_debate = DebateConfig.model_construct(confidence_threshold=0)
        pipeline = PipelineConfig.model_construct(
            description="d", mode="cli", effort=None, stages=[], debate=bad_debate
        )
        with pytest.raises(ValueError, match="Pipeline.*confidence_threshold"):
            validate_pipeline(pipeline, _tasks())

    def test_pipeline_level_above_one_raises(self) -> None:
        bad_debate = DebateConfig.model_construct(confidence_threshold=1.5)
        pipeline = PipelineConfig.model_construct(
            description="d", mode="cli", effort=None, stages=[], debate=bad_debate
        )
        with pytest.raises(ValueError, match="confidence_threshold"):
            validate_pipeline(pipeline, _tasks())

    def test_stage_level_bad_threshold_raises_naming_stage(self) -> None:
        bad_debate = DebateConfig.model_construct(confidence_threshold=-1)
        stage = PipelineStage.model_construct(
            name="Risky Stage",
            task="real-task",
            mode=None,
            model=None,
            effort=None,
            pause_before=False,
            commits_vault=False,
            only_components=None,
            only_tags=None,
            continue_on_failure=False,
            skills=None,
            debate=bad_debate,
        )
        from hivepilot.models import TaskConfig

        tasks = TasksFile(tasks={"real-task": TaskConfig(description="d")})
        pipeline = PipelineConfig(description="d", stages=[stage])
        with pytest.raises(ValueError, match="Risky Stage.*confidence_threshold"):
            validate_pipeline(pipeline, tasks)

    def test_all_absent_or_valid_passes(self) -> None:
        from hivepilot.models import TaskConfig

        stage = PipelineStage(
            name="s", task="real-task", debate=DebateConfig(confidence_threshold=0.7)
        )
        pipeline = PipelineConfig(
            description="d", stages=[stage], debate=DebateConfig(confidence_threshold=0.9)
        )
        tasks = TasksFile(tasks={"real-task": TaskConfig(description="d")})
        validate_pipeline(pipeline, tasks)  # must not raise

    def test_no_debate_blocks_at_all_passes(self) -> None:
        from hivepilot.models import TaskConfig

        stage = PipelineStage(name="s", task="real-task")
        pipeline = PipelineConfig(description="d", stages=[stage])
        tasks = TasksFile(tasks={"real-task": TaskConfig(description="d")})
        validate_pipeline(pipeline, tasks)  # must not raise


class TestReviewFacetParsing:
    """`reviewers` / `review_target` ride the existing `DebateConfig` --
    adversarial-review-thin-layer PRD, Sprint 1. No parallel `ReviewConfig`."""

    def test_reviewers_and_review_target_absent_by_default(self) -> None:
        cfg = DebateConfig()
        assert cfg.reviewers is None
        assert cfg.review_target is None

    def test_reviewers_and_review_target_parse(self) -> None:
        cfg = DebateConfig(reviewers=["alice", "bob"], review_target="github_pr")
        assert cfg.reviewers == ["alice", "bob"]
        assert cfg.review_target == "github_pr"

    def test_review_target_internal_parses(self) -> None:
        cfg = DebateConfig(reviewers=["alice"], review_target="internal")
        assert cfg.review_target == "internal"

    def test_invalid_review_target_literal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DebateConfig.model_validate({"review_target": "bogus"})

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_blank_reviewer_name_rejected(self, blank: str) -> None:
        with pytest.raises(ValidationError):
            DebateConfig(reviewers=["alice", blank])

    def test_review_target_with_explicit_empty_reviewers_rejected_at_load(self) -> None:
        with pytest.raises(ValidationError):
            DebateConfig(review_target="github_pr", reviewers=[])

    def test_review_target_with_unset_reviewers_allowed_at_load(self) -> None:
        # reviewers=None ("unset" / inherit) is NOT the same as reviewers=[]
        # ("explicitly no reviewers") -- only the latter conflicts with a
        # review_target at load time. The unset case is caught later, at
        # resolve time, if it never gets filled in by any layer (see
        # TestReviewFacetFailClosed below).
        cfg = DebateConfig(review_target="internal")
        assert cfg.review_target == "internal"
        assert cfg.reviewers is None


class TestResolveReviewFacetPrecedence:
    def test_review_unset_everywhere_is_regression_safe(self) -> None:
        # No reviewers/review_target anywhere -> behaviorally identical to
        # pre-review `EffectiveDebateConfig` output (byte-identical on the
        # five pre-existing fields; the two new fields resolve to their
        # dormant defaults).
        floor = _FakeFloor(
            enable_debate_judge=True,
            judge_runner="claude",
            judge_model="opus",
            judge_confidence_threshold=0.6,
        )
        pipeline = PipelineConfig(description="d")
        stage = PipelineStage(name="s", task="t")
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result == EffectiveDebateConfig(
            enable_judge=True,
            enable_arbiter=False,
            runner="claude",
            model="opus",
            confidence_threshold=0.6,
            reviewers=[],
            review_target=None,
        )

    def test_pipeline_reviewers_and_target_used_when_stage_unset(self) -> None:
        floor = _FakeFloor()
        pipeline = PipelineConfig(
            description="d", debate=DebateConfig(reviewers=["alice"], review_target="internal")
        )
        stage = PipelineStage(name="s", task="t")
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.reviewers == ["alice"]
        assert result.review_target == "internal"

    def test_stage_reviewers_and_target_override_pipeline(self) -> None:
        floor = _FakeFloor()
        pipeline = PipelineConfig(
            description="d", debate=DebateConfig(reviewers=["alice"], review_target="internal")
        )
        stage = PipelineStage(
            name="s",
            task="t",
            debate=DebateConfig(reviewers=["carol", "dave"], review_target="github_pr"),
        )
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.reviewers == ["carol", "dave"]
        assert result.review_target == "github_pr"

    def test_stage_review_target_inherits_pipeline_reviewers_when_stage_unset(self) -> None:
        floor = _FakeFloor()
        pipeline = PipelineConfig(description="d", debate=DebateConfig(reviewers=["alice"]))
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(review_target="github_pr"))
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.reviewers == ["alice"]
        assert result.review_target == "github_pr"


class TestReviewFacetFailClosed:
    """A resolved `review_target` with an empty resolved `reviewers` list
    must never be treated as a permissive/valid effective config."""

    def test_pipeline_review_target_with_no_reviewers_anywhere_raises(self) -> None:
        # Independently valid at load (reviewers is None/"unset", not an
        # explicit []), but resolves to zero reviewers anywhere in the
        # chain -- the resolve-time guard must catch this.
        floor = _FakeFloor()
        pipeline = PipelineConfig(description="d", debate=DebateConfig(review_target="github_pr"))
        stage = PipelineStage(name="s", task="t")
        with pytest.raises(ValueError, match="reviewers"):
            resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)

    def test_cross_block_empty_stage_reviewers_blocks_pipeline_review_target(self) -> None:
        # Each `debate:` block is independently valid at load time (pipeline
        # sets only review_target, stage sets only an explicit empty
        # reviewers list) -- the per-block model validator cannot see this
        # cross-block combination. resolve_debate_config must still block it.
        floor = _FakeFloor()
        pipeline = PipelineConfig(description="d", debate=DebateConfig(review_target="github_pr"))
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(reviewers=[]))
        with pytest.raises(ValueError, match="reviewers"):
            resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)

    def test_review_target_none_with_empty_reviewers_does_not_raise(self) -> None:
        floor = _FakeFloor()
        pipeline = PipelineConfig(description="d")
        stage = PipelineStage(name="s", task="t")
        result = resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)
        assert result.review_target is None
        assert result.reviewers == []

    def test_stage_empty_reviewers_shadows_pipeline_reviewers_and_blocks(self) -> None:
        # Pipeline supplies BOTH review_target and a non-empty reviewers list;
        # the stage explicitly nulls reviewers with []. The resolver selects
        # reviewers by an explicit `is not None` check (NOT an `or`-fallback),
        # so the stage's explicit [] correctly shadows the pipeline list ->
        # empty -> block. This is the exact empty-value-fail-open the resolver
        # is designed to defeat (a naive `stage or pipeline` would fall through
        # to ["alice"] and silently allow a review-less merge).
        floor = _FakeFloor()
        pipeline = PipelineConfig(
            description="d",
            debate=DebateConfig(review_target="github_pr", reviewers=["alice"]),
        )
        stage = PipelineStage(name="s", task="t", debate=DebateConfig(reviewers=[]))
        with pytest.raises(ValueError, match="reviewers"):
            resolve_debate_config(floor=floor, pipeline=pipeline, stage=stage)

    def test_space_padded_reviewer_name_is_accepted_as_non_empty(self) -> None:
        # The blank-check uses strip() only to DETECT emptiness; a non-blank
        # but space-padded name is a real reviewer and must pass load, so
        # `review_target` + such a name is a valid, non-empty config (it can
        # never later resolve to an empty reviewers list). Locks validator/
        # resolver normalization consistency.
        cfg = DebateConfig(reviewers=[" alice "], review_target="github_pr")
        assert len(cfg.reviewers) == 1
        assert cfg.reviewers[0].strip() == "alice"
