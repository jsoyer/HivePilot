"""Cache that gets created and never read back is money spent twice.

Measured in production across 132 model steps and 43 M prompt-side tokens:
85.0% overall hit rate — healthy. But one step sat at 49.8%, and the detail
repeated on every single run:

    ceo intake   cache_creation ≈ 43 000     cache_read ≈ 16 000

It *creates* 43 000 tokens of cache each time and reads back 16 000.
Creation is billed at 1.25× base input and a read at 0.1×, so a step in that
shape pays close to full price every time.

**Correction (2026-08-08): `ceo intake` is not that step.** Its 15 268-token
read is byte-identical three runs apart, so the prefix is stable and cached
as intended; it simply runs one turn and ends before it can read much back.
A short step falls below 1.0 by construction. The ratio arithmetic below is
right and is what this file tests; the conclusion drawn from it about this
particular step was wrong, and `turns` is what tells the two apart — see
`test_cache_detector_short_steps.py`.

The overall rate hides it completely. 85% looks fine; the step that is
quietly paying double is invisible inside it. So the useful number is not a
rate but a ratio:

    amortisation = cache_read / cache_creation

Below 1.0 means the step created more cache than it has ever read back. That
is a prompt-structure problem — a variable section sitting ahead of a stable
one, so nothing downstream can be cached — and no proxy fixes it.

This module only reports. Which prompt to reorder is the deployment's call;
finding the step that needs it is the engine's.
"""

from __future__ import annotations

import pytest

from hivepilot.services import cache_efficiency, state_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    state_service.init_db()
    return tmp_path


def _step(
    step: str, *, read: int, create: int, inp: int = 100, n: int = 1, turns: int | None = 12
) -> None:
    """`turns` defaults high on purpose.

    These tests are about the ratio arithmetic — the median, the outlier, the
    ordering — and a low ratio is only a finding when the step ran enough
    turns for the prefix to have been read back. Supplying a many-turn count
    keeps each case in the bucket it was written to exercise. The short-step
    and unknown-turns paths have their own file.
    """
    for _ in range(n):
        run = state_service.record_run_start("p", "t")
        state_service.record_step(
            run,
            step,
            "success",
            provider="claude",
            model="claude-opus-5",
            input_tokens=inp,
            cache_read_tokens=read,
            cache_creation_tokens=create,
            turns=turns,
        )


class TestTheOverallPicture:
    def test_it_reports_a_hit_rate(self, db) -> None:
        _step("review", read=900, create=0, inp=100)

        assert cache_efficiency.cache_summary()["hit_rate"] == pytest.approx(0.9)

    def test_no_data_is_reported_as_none_not_zero(self, db) -> None:
        """A rate of 0.0 reads as "the cache never works". Absent data is a
        different statement and must look different."""
        summary = cache_efficiency.cache_summary()

        assert summary["hit_rate"] is None
        assert summary["steps"] == 0

    def test_steps_without_a_model_are_excluded(self, db) -> None:
        """A shell step has no prompt and no cache; counting it would dilute
        the rate with rows that could never have a cache to hit."""
        _step("review", read=900, create=0, inp=100)
        run = state_service.record_run_start("p", "t")
        state_service.record_step(run, "build", "success", provider="shell")

        assert cache_efficiency.cache_summary()["steps"] == 1


class TestFindingTheStepThatPaysTwice:
    def test_a_step_that_never_amortises_is_flagged(self, db) -> None:
        """Creates far more than it reads, every run, over many turns — so
        the prefix could have been read back and was not.

        Named `ceo intake` when this was written, on the belief that the
        production step was pathological. It is not: it runs one turn, which
        puts it below the floor by construction. See
        `test_cache_detector_short_steps.py`. The arithmetic under test here
        is unchanged; only the example was wrong.
        """
        _step("ceo intake", read=16000, create=43000, n=5)

        flagged = [s["step"] for s in cache_efficiency.cache_summary()["unamortised"]]

        assert "ceo intake" in flagged

    def test_a_healthy_step_is_not_flagged(self, db) -> None:
        _step("implementation", read=90000, create=10000, n=5)

        flagged = [s["step"] for s in cache_efficiency.cache_summary()["unamortised"]]

        assert "implementation" not in flagged

    def test_it_reports_the_ratio_not_a_verdict(self, db) -> None:
        """ "Wasted 800k tokens" claims to know what the prefix should have
        been. "Created 215k, read back 80k" is what actually happened."""
        _step("ceo intake", read=16000, create=43000, n=5)

        entry = cache_efficiency.cache_summary()["unamortised"][0]

        assert entry["cache_creation"] == 215000
        assert entry["cache_read"] == 80000
        assert entry["amortisation"] == pytest.approx(80000 / 215000)

    def test_a_step_seen_once_is_never_flagged(self, db) -> None:
        """The first run of anything has nothing to reuse yet. Flagging it
        would report a cold start as a defect."""
        _step("brand new", read=0, create=50000, n=1)

        assert cache_efficiency.cache_summary()["unamortised"] == []

    def test_the_worst_offender_comes_first(self, db) -> None:
        _step("mildly bad", read=8000, create=10000, n=4)
        _step("terrible", read=1000, create=50000, n=4)

        flagged = [s["step"] for s in cache_efficiency.cache_summary()["unamortised"]]

        assert flagged[0] == "terrible"

    def test_one_lucky_run_does_not_rescue_a_bad_step(self, db) -> None:
        """The first version of this summed across runs and missed the real
        case entirely.

        In production `ceo intake` creates ~43 000 and reads ~16 000 on nine
        runs out of ten — and one outlier read 326 696, which lifted the
        *sum* above 1.0 and un-flagged the step. Summing hid the pathology
        exactly the way the global 85% hit rate hid the step: the same
        mistake, one level down. The median per run is what survives an
        outlier.
        """
        _step("ceo intake", read=16000, create=43000, n=9)
        _step("ceo intake", read=326696, create=46554, n=1)

        flagged = [s["step"] for s in cache_efficiency.cache_summary()["unamortised"]]

        assert "ceo intake" in flagged

    def test_a_step_that_is_usually_fine_is_not_flagged_for_one_bad_run(self, db) -> None:
        """The rule has to cut both ways, or it just trades false negatives
        for false positives."""
        _step("implementation", read=90000, create=10000, n=9)
        _step("implementation", read=0, create=50000, n=1)

        flagged = [s["step"] for s in cache_efficiency.cache_summary()["unamortised"]]

        assert "implementation" not in flagged

    def test_a_step_that_creates_nothing_is_not_flagged(self, db) -> None:
        """Dividing by zero creation is not a 0-amortisation step — it is a
        step with no cache to amortise."""
        _step("cheap", read=0, create=0, n=4)

        assert cache_efficiency.cache_summary()["unamortised"] == []


class TestItJoinsTheEfficiencyView:
    def test_the_composed_summary_names_cache(self, db, monkeypatch) -> None:
        """The module's own rule: a view that composes savings sources has to
        name every one of them, or the total it implies is wrong. Cache was
        the one it did not name."""
        from hivepilot.services import efficiency_service

        assert "cache" in efficiency_service.efficiency_summary()
