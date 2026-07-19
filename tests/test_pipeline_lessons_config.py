"""Tests for the YAML-facing lessons-loop config layer
(per-pipeline-lessons-yaml PRD, Sprint 1).

Covers:
- `LessonsConfig` model parsing (absent / full / partial `lessons:` blocks).
- `LessonsConfig.min_score`/`inject_limit`/`distill_runner`/`distill_model`
  field-validator matrices -- the load-time guards that a bad value can never
  reach `resolve_lessons_config`.
- `resolve_lessons_config`'s HYBRIDE precedence: strengthen-only OR across the
  two enable flags (a pipeline `False`/absent can never turn OFF a floor
  `True`), pipeline-overrides-floor for the four scalars, first-non-None wins.
- `validate_lessons_config`'s defense-in-depth re-check of `min_score`/
  `inject_limit` (using `model_construct` to simulate a value that slipped
  past the pydantic validator).

This sprint is a pure, dormant config layer -- no consumption-site wiring. A
`lessons:` block in pipelines.yaml is inert until a later sprint threads
`resolve_lessons_config` into the distillation/retrieval call sites.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from hivepilot.models import (
    EffectiveLessonsConfig,
    LessonsConfig,
    PipelineConfig,
    TasksFile,
    resolve_lessons_config,
)
from hivepilot.services.pipeline_service import validate_lessons_config, validate_pipeline


@dataclass
class _FakeFloor:
    """Lightweight test double for the global settings floor -- exposes only
    the six fields `resolve_lessons_config` reads. Does NOT depend on the
    real `hivepilot.config.settings` singleton, so these tests are immune to
    whatever the real defaults happen to be."""

    enable_lesson_distillation: bool = False
    enable_semantic_lesson_retrieval: bool = False
    lesson_distill_runner: str = "claude"
    lesson_distill_model: str | None = None
    lesson_min_score: float = 0.5
    lesson_inject_limit: int = 5


def _tasks() -> TasksFile:
    return TasksFile(tasks={})


class TestLessonsConfigParsing:
    def test_lessons_absent_is_none(self) -> None:
        pipeline = PipelineConfig(description="d")
        assert pipeline.lessons is None

    def test_full_block_parses(self) -> None:
        cfg = LessonsConfig(
            enable_distillation=True,
            enable_semantic=True,
            distill_runner="codex",
            distill_model="gpt-5",
            min_score=0.75,
            inject_limit=3,
        )
        assert cfg.enable_distillation is True
        assert cfg.enable_semantic is True
        assert cfg.distill_runner == "codex"
        assert cfg.distill_model == "gpt-5"
        assert cfg.min_score == 0.75
        assert cfg.inject_limit == 3

    def test_partial_block_parses_rest_none(self) -> None:
        cfg = LessonsConfig(enable_distillation=True)
        assert cfg.enable_distillation is True
        assert cfg.enable_semantic is None
        assert cfg.distill_runner is None
        assert cfg.distill_model is None
        assert cfg.min_score is None
        assert cfg.inject_limit is None

    def test_pipeline_accepts_lessons_block(self) -> None:
        pipeline = PipelineConfig(description="d", lessons=LessonsConfig(enable_distillation=True))
        assert pipeline.lessons is not None
        assert pipeline.lessons.enable_distillation is True


class TestMinScoreValidatorMatrix:
    @pytest.mark.parametrize("bad_value", [0, -0.1, -1, 1.0001, 2, math.nan, math.inf, -math.inf])
    def test_rejects_out_of_range(self, bad_value: float) -> None:
        with pytest.raises(ValidationError):
            LessonsConfig(min_score=bad_value)

    @pytest.mark.parametrize("good_value", [0.5, 1.0, 0.0001])
    def test_accepts_in_range(self, good_value: float) -> None:
        cfg = LessonsConfig(min_score=good_value)
        assert cfg.min_score == good_value

    def test_none_is_accepted(self) -> None:
        cfg = LessonsConfig(min_score=None)
        assert cfg.min_score is None

    def test_absent_is_accepted(self) -> None:
        cfg = LessonsConfig()
        assert cfg.min_score is None


class TestInjectLimitValidatorMatrix:
    @pytest.mark.parametrize("bad_value", [0, -1, -5])
    def test_rejects_below_one(self, bad_value: int) -> None:
        with pytest.raises(ValidationError):
            LessonsConfig(inject_limit=bad_value)

    @pytest.mark.parametrize("good_value", [1, 5, 100])
    def test_accepts_at_least_one(self, good_value: int) -> None:
        cfg = LessonsConfig(inject_limit=good_value)
        assert cfg.inject_limit == good_value

    def test_none_is_accepted(self) -> None:
        cfg = LessonsConfig(inject_limit=None)
        assert cfg.inject_limit is None


class TestBlankOverrideValidatorMatrix:
    @pytest.mark.parametrize("field", ["distill_runner", "distill_model"])
    @pytest.mark.parametrize("bad_value", ["", "   ", "\t\n"])
    def test_rejects_blank(self, field: str, bad_value: str) -> None:
        with pytest.raises(ValidationError):
            LessonsConfig(**{field: bad_value})

    @pytest.mark.parametrize("field", ["distill_runner", "distill_model"])
    def test_accepts_none(self, field: str) -> None:
        cfg = LessonsConfig(**{field: None})
        assert getattr(cfg, field) is None

    @pytest.mark.parametrize("field", ["distill_runner", "distill_model"])
    def test_accepts_non_blank(self, field: str) -> None:
        cfg = LessonsConfig(**{field: "codex"})
        assert getattr(cfg, field) == "codex"


class TestResolveLessonsConfigPrecedence:
    def test_floor_only_returns_floor_values(self) -> None:
        floor = _FakeFloor(
            enable_lesson_distillation=True,
            enable_semantic_lesson_retrieval=False,
            lesson_distill_runner="claude",
            lesson_distill_model="opus",
            lesson_min_score=0.6,
            lesson_inject_limit=7,
        )
        result = resolve_lessons_config(floor=floor, pipeline=None)
        assert result == EffectiveLessonsConfig(
            enable_distillation=True,
            enable_semantic=False,
            distill_runner="claude",
            distill_model="opus",
            min_score=0.6,
            inject_limit=7,
        )

    def test_pipeline_overrides_scalars_over_floor(self) -> None:
        floor = _FakeFloor(
            lesson_distill_runner="claude",
            lesson_distill_model=None,
            lesson_min_score=0.5,
            lesson_inject_limit=5,
        )
        pipeline = PipelineConfig(
            description="d",
            lessons=LessonsConfig(
                distill_runner="codex", distill_model="gpt-5", min_score=0.8, inject_limit=2
            ),
        )
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.distill_runner == "codex"
        assert result.distill_model == "gpt-5"
        assert result.min_score == 0.8
        assert result.inject_limit == 2

    def test_scalars_absent_inherit_floor(self) -> None:
        floor = _FakeFloor(
            lesson_distill_runner="claude",
            lesson_distill_model="opus",
            lesson_min_score=0.42,
            lesson_inject_limit=9,
        )
        pipeline = PipelineConfig(description="d", lessons=LessonsConfig(enable_distillation=True))
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.distill_runner == "claude"
        assert result.distill_model == "opus"
        assert result.min_score == 0.42
        assert result.inject_limit == 9

    def test_enable_distillation_pipeline_false_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_lesson_distillation=True)
        pipeline = PipelineConfig(description="d", lessons=LessonsConfig(enable_distillation=False))
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.enable_distillation is True

    def test_enable_distillation_absent_block_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_lesson_distillation=True)
        pipeline = PipelineConfig(description="d")  # lessons=None
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.enable_distillation is True

    def test_enable_semantic_pipeline_false_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_semantic_lesson_retrieval=True)
        pipeline = PipelineConfig(description="d", lessons=LessonsConfig(enable_semantic=False))
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.enable_semantic is True

    def test_enable_semantic_absent_block_cannot_switch_off_floor_true(self) -> None:
        floor = _FakeFloor(enable_semantic_lesson_retrieval=True)
        pipeline = PipelineConfig(description="d")  # lessons=None
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.enable_semantic is True

    def test_pipeline_enable_distillation_turns_on_when_floor_false(self) -> None:
        floor = _FakeFloor(enable_lesson_distillation=False)
        pipeline = PipelineConfig(description="d", lessons=LessonsConfig(enable_distillation=True))
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.enable_distillation is True

    def test_pipeline_enable_semantic_turns_on_when_floor_false(self) -> None:
        floor = _FakeFloor(enable_semantic_lesson_retrieval=False)
        pipeline = PipelineConfig(description="d", lessons=LessonsConfig(enable_semantic=True))
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result.enable_semantic is True

    def test_none_lessons_on_pipeline_is_guarded(self) -> None:
        floor = _FakeFloor(
            enable_lesson_distillation=True,
            lesson_distill_runner="claude",
            lesson_distill_model="opus",
            lesson_min_score=0.5,
            lesson_inject_limit=5,
        )
        pipeline = PipelineConfig(description="d")  # lessons=None
        result = resolve_lessons_config(floor=floor, pipeline=pipeline)
        assert result == EffectiveLessonsConfig(
            enable_distillation=True,
            enable_semantic=False,
            distill_runner="claude",
            distill_model="opus",
            min_score=0.5,
            inject_limit=5,
        )

    def test_pipeline_none_uses_floor_only(self) -> None:
        floor = _FakeFloor(
            enable_lesson_distillation=True,
            lesson_distill_runner="claude",
            lesson_min_score=0.5,
            lesson_inject_limit=5,
        )
        result = resolve_lessons_config(floor=floor, pipeline=None)
        assert result.enable_distillation is True
        assert result.distill_runner == "claude"

    def test_default_floor_param_uses_real_settings_singleton(self) -> None:
        # No `floor` passed -> lazily imports hivepilot.config.settings. Just
        # prove it doesn't raise and returns a well-formed, positive-finite
        # min_score/inject_limit; we don't assert on the real settings'
        # concrete values since this test must stay immune to their defaults
        # changing.
        result = resolve_lessons_config(pipeline=None)
        assert isinstance(result, EffectiveLessonsConfig)
        assert math.isfinite(result.min_score)
        assert result.min_score > 0
        assert result.inject_limit >= 1


class TestValidateLessonsConfig:
    def test_pipeline_level_bad_min_score_raises(self) -> None:
        bad_lessons = LessonsConfig.model_construct(min_score=0)
        pipeline = PipelineConfig.model_construct(
            description="d", mode="cli", effort=None, stages=[], lessons=bad_lessons
        )
        with pytest.raises(ValueError, match="Pipeline.*min_score"):
            validate_lessons_config(pipeline)

    def test_pipeline_level_min_score_above_one_raises(self) -> None:
        bad_lessons = LessonsConfig.model_construct(min_score=1.5)
        pipeline = PipelineConfig.model_construct(
            description="d", mode="cli", effort=None, stages=[], lessons=bad_lessons
        )
        with pytest.raises(ValueError, match="min_score"):
            validate_lessons_config(pipeline)

    def test_pipeline_level_bad_inject_limit_raises(self) -> None:
        bad_lessons = LessonsConfig.model_construct(inject_limit=0)
        pipeline = PipelineConfig.model_construct(
            description="d", mode="cli", effort=None, stages=[], lessons=bad_lessons
        )
        with pytest.raises(ValueError, match="Pipeline.*inject_limit"):
            validate_lessons_config(pipeline)

    def test_all_absent_or_valid_passes(self) -> None:
        pipeline = PipelineConfig(
            description="d", lessons=LessonsConfig(min_score=0.7, inject_limit=3)
        )
        validate_lessons_config(pipeline)  # must not raise

    def test_no_lessons_block_passes(self) -> None:
        pipeline = PipelineConfig(description="d")
        validate_lessons_config(pipeline)  # must not raise

    def test_wired_into_validate_pipeline(self) -> None:

        bad_lessons = LessonsConfig.model_construct(min_score=0)
        pipeline = PipelineConfig.model_construct(
            description="d", mode="cli", effort=None, stages=[], lessons=bad_lessons
        )
        with pytest.raises(ValueError, match="min_score"):
            validate_pipeline(pipeline, _tasks())

    def test_validate_pipeline_passes_with_valid_lessons(self) -> None:

        stage_free_pipeline = PipelineConfig(description="d", lessons=LessonsConfig(min_score=0.9))
        validate_pipeline(stage_free_pipeline, _tasks())  # must not raise
