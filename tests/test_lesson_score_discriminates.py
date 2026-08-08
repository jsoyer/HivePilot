"""A score that is always 1.0 ranks nothing.

Measured on the box once distillation finally ran (#447): **100 lessons,
every one `score = 1.0`, every one `validated = 1`, every `confidence`
NULL**. So:

- `lesson_min_score = 0.5` filters nothing;
- `validated` carries no information, and the fail-closed anti-poisoning
  gate the module was built around gates nothing in practice;
- `retrieve_lessons` picks `lesson_inject_limit = 5` out of 100 by no
  criterion worth the name — which is the actual blocker on agents learning
  anything, since what they get injected is arbitrary.

The cause is the scoring rule, not the plumbing. `validate_lesson` takes the
MAX of the present signals and gives `run_success=True` a flat **1.0**.
Almost every run succeeds, so almost every lesson saturates, and the one
genuinely discriminating input — a judge's confidence on a resolved
challenge — can never raise a lesson above its neighbours because they are
already at the ceiling.

`run_success` is also the *weakest* thing we know. That a fifteen-stage
pipeline finished says close to nothing about whether one distilled sentence
is correct; a reviewer's confidence on a challenge that was actually
adjudicated says a great deal. Ranking them identically discards the
difference.

So: `run_success` alone now scores at the admission floor rather than the
ceiling. Nothing that used to be admitted becomes quarantined — the
fail-closed property is untouched — but a lesson backed by real judgement
now outranks one backed only by "the run did not crash", and
`lesson_min_score` becomes a dial that does something.
"""

from __future__ import annotations

import pytest

from hivepilot.services.lessons_service import (
    _RUN_SUCCESS_SCORE,
    Lesson,
    OutcomeSignal,
    validate_lesson,
)


def _lesson() -> Lesson:
    return Lesson(text="always grep the repo before planning", category="planning")


class TestRunSuccessNoLongerSaturates:
    def test_run_success_alone_does_not_reach_the_ceiling(self) -> None:
        validated, score = validate_lesson(
            _lesson(), OutcomeSignal(run_success=True), min_score=0.5
        )

        assert validated, "it must still be admitted — this is not a tightening"
        assert score < 1.0

    def test_a_judged_lesson_outranks_a_merely_successful_one(self) -> None:
        """The whole point: two lessons from two runs must be orderable."""
        _, plain = validate_lesson(_lesson(), OutcomeSignal(run_success=True), min_score=0.5)
        _, judged = validate_lesson(
            _lesson(),
            OutcomeSignal(run_success=True, resolved_challenge=True, max_verdict_confidence=0.9),
            min_score=0.5,
        )

        assert judged > plain

    def test_a_high_confidence_verdict_still_wins_over_a_low_one(self) -> None:
        _, low = validate_lesson(
            _lesson(), OutcomeSignal(run_success=True, max_verdict_confidence=0.6), min_score=0.5
        )
        _, high = validate_lesson(
            _lesson(), OutcomeSignal(run_success=True, max_verdict_confidence=0.95), min_score=0.5
        )

        assert high > low

    def test_min_score_becomes_a_working_dial(self) -> None:
        """With everything at 1.0 the floor could never exclude anything.
        Raising it above the run-success score must now quarantine lessons
        that have nothing but a finished run behind them."""
        validated, _ = validate_lesson(
            _lesson(), OutcomeSignal(run_success=True), min_score=_RUN_SUCCESS_SCORE + 0.1
        )

        assert not validated


class TestFailClosedIsUntouched:
    def test_no_signal_is_still_denied(self) -> None:
        validated, score = validate_lesson(_lesson(), OutcomeSignal(), min_score=0.5)

        assert not validated
        assert score == 0.0

    def test_none_signal_is_still_denied(self) -> None:
        validated, score = validate_lesson(_lesson(), None, min_score=0.5)

        assert not validated
        assert score == 0.0

    def test_a_resolved_challenge_still_reaches_the_ceiling(self) -> None:
        """An adjudicated challenge is the strongest signal the module has;
        it keeps the top of the scale."""
        _, score = validate_lesson(_lesson(), OutcomeSignal(resolved_challenge=True), min_score=0.5)

        assert score == 1.0

    def test_an_out_of_range_confidence_is_ignored_not_trusted(self) -> None:
        """Unchanged guard: a nonsense confidence must not become a score."""
        _, score = validate_lesson(
            _lesson(),
            OutcomeSignal(run_success=True, max_verdict_confidence=42.0),
            min_score=0.5,
        )

        assert score == pytest.approx(_RUN_SUCCESS_SCORE)

    def test_the_run_success_score_sits_at_the_default_floor(self) -> None:
        """Chosen so nothing that was admitted before becomes quarantined by
        this change alone — the default `lesson_min_score` is 0.5."""
        assert _RUN_SUCCESS_SCORE == 0.5
