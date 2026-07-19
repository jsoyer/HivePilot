"""
Unit tests for `hivepilot.services.lessons_service.validate_lesson` (Auto-
Learning Lessons Loop PRD, Sprint 3) -- the fail-closed anti-poisoning gate
that decides whether a distilled lesson CANDIDATE becomes a retrievable,
injectable VALIDATED lesson.

Covers the FAIL-CLOSED matrix that is the core security property of this
Sprint:
- A failed run alone -> quarantined.
- A below-`lesson_min_score` verdict confidence -> quarantined.
- NO signal at all -- `outcome_signal=None`, or a default/empty
  `OutcomeSignal()` -- -> quarantined. This is the explicit
  empty-value-fail-open guard: an absent/empty outcome must be treated as
  DENY, never as "no constraint -> allow" (the recurring HivePilot bug
  class this module exists to avoid).
- A non-finite (NaN/inf) confidence is ignored, not trusted, as defense in
  depth against a malformed upstream value.

And the positive path:
- A success run / a resolved challenge / a verdict confidence at or above
  `lesson_min_score` -> validated, with the returned score coming from the
  REAL OUTCOME -- proven not just behaviorally but at the type level: the
  `Lesson` candidate dataclass has no `score`/`confidence` field for a
  distiller self-report to leak through in the first place (mirrors the
  same invariant `tests/test_lessons_service.py::
  TestParseDistilledLessons::test_self_reported_score_and_confidence_are_ignored`
  locks in for `parse_distilled_lessons`).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hivepilot.config import settings
from hivepilot.services.lessons_service import Lesson, OutcomeSignal, validate_lesson

_LESSON = Lesson(text="Always run tests before merging.", category="testing")


@pytest.fixture(autouse=True)
def _reset_lesson_min_score() -> Iterator[None]:
    """Guarantee `lesson_min_score` overrides never leak between tests."""
    original = settings.lesson_min_score
    yield
    settings.lesson_min_score = original


class TestFailClosedQuarantine:
    """The core anti-poisoning property: absent/insufficient real signal
    must DENY, never silently ALLOW."""

    def test_failed_run_alone_is_quarantined(self) -> None:
        signal = OutcomeSignal(run_success=False)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is False
        assert score == 0.0

    def test_below_threshold_verdict_confidence_is_quarantined(self) -> None:
        settings.lesson_min_score = 0.5
        signal = OutcomeSignal(max_verdict_confidence=0.3)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is False
        # Score is still the real (sub-threshold) confidence -- quarantined
        # means "not validated", not "score discarded".
        assert score == 0.3

    def test_none_outcome_signal_is_quarantined(self) -> None:
        """THE empty-value-fail-open guard: a caller passing no signal at
        all (`None`, not even a default `OutcomeSignal()`) must be denied,
        never read as 'no constraint -> allow'."""
        validated, score = validate_lesson(_LESSON, None)
        assert validated is False
        assert score == 0.0

    def test_default_empty_outcome_signal_is_quarantined(self) -> None:
        """Same guard via the typed dataclass's own DENY defaults (every
        field False/False/None) rather than `None` outright -- both shapes
        of 'empty' must quarantine."""
        signal = OutcomeSignal()
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is False
        assert score == 0.0

    def test_non_finite_confidence_is_not_trusted(self) -> None:
        """A NaN confidence (malformed upstream value) must not be read as
        real signal -- defense in depth, not just an absent value."""
        signal = OutcomeSignal(max_verdict_confidence=float("nan"))
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is False
        assert score == 0.0

    def test_out_of_range_confidence_is_not_trusted(self) -> None:
        signal = OutcomeSignal(max_verdict_confidence=1.5)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is False
        assert score == 0.0

    def test_misconfigured_zero_floor_cannot_be_set(self) -> None:
        """Defense in depth beyond `validate_lesson` itself: even a caller
        that tries to defeat the gate by zeroing `lesson_min_score` can't --
        `Settings._validate_lesson_min_score` fail-closes at construction.
        Documented here as the sibling guarantee to this module's own
        fail-closed signal handling (see `OutcomeSignal`'s docstring)."""
        from pydantic import ValidationError

        from hivepilot.config import Settings

        with pytest.raises(ValidationError):
            Settings(lesson_min_score=0.0)


class TestValidatedFromRealOutcome:
    """A genuinely outcome-backed candidate validates, and its score is
    always the OUTCOME's, never the distiller's self-report."""

    def test_lesson_candidate_has_no_score_field_to_leak(self) -> None:
        assert not hasattr(_LESSON, "score")
        assert not hasattr(_LESSON, "confidence")

    def test_success_run_validates_with_score_from_outcome(self) -> None:
        signal = OutcomeSignal(run_success=True)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is True
        assert score == 1.0

    def test_resolved_challenge_validates(self) -> None:
        signal = OutcomeSignal(resolved_challenge=True)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is True
        assert score == 1.0

    def test_verdict_confidence_at_threshold_validates_with_that_score(self) -> None:
        settings.lesson_min_score = 0.5
        signal = OutcomeSignal(max_verdict_confidence=0.7)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is True
        assert score == 0.7

    def test_verdict_confidence_exactly_at_floor_validates(self) -> None:
        """Boundary: `>=`, not `>` -- a confidence exactly at the floor
        must validate, matching `lesson_min_score`'s own (0, 1] semantics."""
        settings.lesson_min_score = 0.5
        signal = OutcomeSignal(max_verdict_confidence=0.5)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is True
        assert score == 0.5

    def test_score_is_max_of_multiple_real_signals(self) -> None:
        settings.lesson_min_score = 0.5
        signal = OutcomeSignal(run_success=True, max_verdict_confidence=0.3)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is True
        # run_success (1.0) wins over the lower confidence signal.
        assert score == 1.0

    def test_score_never_equals_a_distiller_self_report(self) -> None:
        """A `Lesson` built from a distiller response that (illegally, per
        `parse_distilled_lessons`) tried to smuggle a self-reported
        score/confidence still has none of those fields -- so the ONLY
        value `validate_lesson` can possibly return is the outcome's own,
        here 0.9, never e.g. 0.99 the way a malicious/buggy distiller
        response claimed in `tests/test_lessons_distillation.py`."""
        signal = OutcomeSignal(max_verdict_confidence=0.9)
        validated, score = validate_lesson(_LESSON, signal)
        assert validated is True
        assert score == 0.9
        assert score != 0.99
