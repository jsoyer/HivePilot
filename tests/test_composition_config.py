"""Enabling team composition must take BOTH layers saying yes.

Every other per-pipeline block in this codebase resolves with a strengthen-only
OR: `debate:` and `lessons:` can only ADD gating, so a pipeline `True` turning
on a floor `False` is fail-closed, and the OR is right.

Composition inverts that. It REMOVES stages -- it is the one block whose
"enabled" is the permissive state. An OR here would mean any single layer could
switch off the CISO for a pipeline, and a config repo that a run can write to
would be one commit away from doing so.

So the floor is a PERMISSION, not a default, and resolution is an AND: the
deployment must allow it and the pipeline must ask for it. Either silent, and
the full roster runs.
"""

from __future__ import annotations

import pytest

from hivepilot.models import CompositionConfig, PipelineConfig, resolve_composition_config


class _Floor:
    def __init__(self, allow: bool) -> None:
        self.allow_team_composition = allow


def _pipeline(composition: CompositionConfig | None) -> PipelineConfig:
    return PipelineConfig(description="d", composition=composition)


class TestBothLayersAreRequired:
    def test_neither_is_off(self):
        effective = resolve_composition_config(floor=_Floor(False), pipeline=_pipeline(None))

        assert effective.enabled is False

    def test_the_floor_alone_is_off(self):
        """A deployment-wide permission is not a request. Turning the floor on
        must not silently shrink every pipeline."""
        effective = resolve_composition_config(floor=_Floor(True), pipeline=_pipeline(None))

        assert effective.enabled is False

    def test_the_pipeline_alone_is_off(self):
        """The inversion that matters: unlike `debate:`, a pipeline cannot
        enable this on its own. The deployment has to permit it."""
        effective = resolve_composition_config(
            floor=_Floor(False), pipeline=_pipeline(CompositionConfig(enabled=True))
        )

        assert effective.enabled is False

    def test_both_is_on(self):
        """A selector is part of "the pipeline asks for it" -- see
        TestTheSelectorStage below for why enabling without one is off."""
        effective = resolve_composition_config(
            floor=_Floor(True),
            pipeline=_pipeline(CompositionConfig(enabled=True, selector_stage="Plan Synthesis")),
        )

        assert effective.enabled is True

    def test_an_explicit_pipeline_false_stays_off_under_a_permitting_floor(self):
        effective = resolve_composition_config(
            floor=_Floor(True),
            pipeline=_pipeline(CompositionConfig(enabled=False, selector_stage="Plan Synthesis")),
        )

        assert effective.enabled is False


class TestTheSelectorStage:
    def test_it_carries_through(self):
        effective = resolve_composition_config(
            floor=_Floor(True),
            pipeline=_pipeline(CompositionConfig(enabled=True, selector_stage="Plan Synthesis")),
        )

        assert effective.selector_stage == "Plan Synthesis"

    def test_composition_without_a_selector_is_off(self):
        """Nothing would ever propose a team, so 'enabled' would be a lie -- and
        a lie that reads as 'a smaller team was chosen' rather than 'nobody
        chose'."""
        effective = resolve_composition_config(
            floor=_Floor(True), pipeline=_pipeline(CompositionConfig(enabled=True))
        )

        assert effective.enabled is False


class TestTheDefaultIsOff:
    def test_a_pipeline_declaring_nothing_has_no_composition(self):
        assert PipelineConfig(description="d").composition is None

    @pytest.mark.parametrize("floor_allows", [True, False])
    def test_and_resolves_off_either_way(self, floor_allows):
        effective = resolve_composition_config(
            floor=_Floor(floor_allows), pipeline=PipelineConfig(description="d")
        )

        assert effective.enabled is False
