"""A short step cannot amortise its cache, and that is not a defect.

The detector flags any step whose median `cache_read / cache_creation` sits
below 1.0 and calls it a prompt-structure problem that reordering would fix.
Production data says otherwise. `ceo intake`, across four real runs:

    run 391   input 4 482   read 15 268   created 45 517   -> 0.335
    run 362   input 4 051   read 15 268   created 44 943   -> 0.340
    run 305   input 3 719   read 15 268   created 43 773   -> 0.349

against `implementation` on run 396: read 50 465 734 for 334 906 created,
which is **150.7**.

Two things follow from that 15 268. It is read back *within* the dispatch,
and it is identical three runs apart — so the prefix is stable and is being
cached exactly as intended. The gap between 0.34 and 150.7 is therefore not
prompt quality: it is how many turns the step runs. `implementation` reads
its prefix back on each of many turns. `ceo intake` is a short first stage
that writes its prompt once and finishes before it can read much of it back.

A short step therefore sits below 1.0 *by construction*, however well its
prompt is ordered — and the detector was sending an operator to reorder a
prompt that was already correct. #444 made that worse by putting the table
in front of one.

`num_turns` is the discriminator. It is already in the envelope the runner
parses, and was simply never stored. Rows written before it was captured
stay NULL and are counted apart rather than guessed at: "we cannot tell" is
a different statement from "this is fine", and neither one is "this is
broken".
"""

from __future__ import annotations

import json

import pytest

from hivepilot.runners.claude_runner import _parse_usage_envelope
from hivepilot.services import cache_efficiency, state_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    state_service.init_db()
    return tmp_path


def _step(step: str, *, read: int, create: int, turns: int | None, n: int = 3) -> None:
    for _ in range(n):
        run = state_service.record_run_start("p", "t")
        state_service.record_step(
            run,
            step,
            "success",
            provider="claude",
            model="claude-opus-5",
            input_tokens=100,
            cache_read_tokens=read,
            cache_creation_tokens=create,
            turns=turns,
        )


def _envelope(**extra: object) -> str:
    return json.dumps({"result": "ok", "total_cost_usd": 0.5, **extra})


class TestTurnsAreCaptured:
    def test_num_turns_reaches_usage(self) -> None:
        parsed = _parse_usage_envelope(_envelope(num_turns=7))

        assert parsed is not None
        assert parsed[1].turns == 7

    def test_it_survives_the_modelusage_path(self) -> None:
        """`num_turns` is top-level while tokens come from `modelUsage`, so
        the primary path has to reach outside its own sub-object for it."""
        parsed = _parse_usage_envelope(
            _envelope(
                num_turns=4,
                modelUsage={
                    "claude-opus-5": {
                        "inputTokens": 100,
                        "outputTokens": 10,
                        "cacheReadInputTokens": 5,
                        "cacheCreationInputTokens": 5,
                        "costUSD": 0.5,
                        "canonicalModel": "claude-opus-5",
                    }
                },
            )
        )

        assert parsed is not None
        assert parsed[1].turns == 4

    def test_absent_num_turns_is_none_not_zero(self) -> None:
        """Zero turns asserts "the agent did nothing". A CLI that does not
        report the field has asserted nothing at all."""
        parsed = _parse_usage_envelope(_envelope())

        assert parsed is not None
        assert parsed[1].turns is None

    def test_nonsense_num_turns_is_rejected(self) -> None:
        parsed = _parse_usage_envelope(_envelope(num_turns="three"))

        assert parsed is not None
        assert parsed[1].turns is None


class TestShortStepsAreNotCalledPathological:
    def test_a_one_turn_step_is_not_flagged(self, db) -> None:
        """`ceo intake`'s real shape. Nothing to fix, so nothing to report
        as fixable."""
        _step("ceo intake", read=15268, create=45517, turns=1)

        summary = cache_efficiency.cache_summary()

        assert [e["step"] for e in summary["unamortised"]] == []
        assert [e["step"] for e in summary["single_pass"]] == ["ceo intake"]

    def test_a_many_turn_step_below_the_floor_is_still_flagged(self, db) -> None:
        """The pathology the module exists to find: the prefix COULD have
        been read back on each of many turns, and was not."""
        _step("bad prompt", read=1_000, create=90_000, turns=12)

        summary = cache_efficiency.cache_summary()

        assert [e["step"] for e in summary["unamortised"]] == ["bad prompt"]
        assert summary["single_pass"] == []

    def test_a_healthy_step_appears_in_neither_list(self, db) -> None:
        _step("implementation", read=500_000, create=3_000, turns=20)

        summary = cache_efficiency.cache_summary()

        assert summary["unamortised"] == []
        assert summary["single_pass"] == []

    def test_the_reported_turns_travel_with_the_finding(self, db) -> None:
        """An operator asked to reorder a prompt deserves the evidence that
        reordering could have helped."""
        _step("bad prompt", read=1_000, create=90_000, turns=12)

        assert cache_efficiency.cache_summary()["unamortised"][0]["turns"] == 12


class TestNothingIsGuessed:
    def test_rows_predating_turn_capture_are_counted_not_classified(self, db) -> None:
        """Every historic row has NULL turns. Folding them into either
        bucket would invent the very fact the column was added to supply."""
        _step("legacy", read=10, create=1_000, turns=None)

        summary = cache_efficiency.cache_summary()

        assert summary["unamortised"] == []
        assert summary["single_pass"] == []
        assert summary["unclassified"] == 1

    def test_a_healthy_legacy_row_is_not_counted_as_unclassified(self, db) -> None:
        """Only steps below the floor need classifying at all. A step above
        it is fine whatever its turn count, so it is not an open question."""
        _step("legacy fine", read=1_000, create=10, turns=None)

        assert cache_efficiency.cache_summary()["unclassified"] == 0

    def test_the_global_hit_rate_is_unchanged_by_the_split(self, db) -> None:
        """The split is about attribution, not measurement. A step moving
        between buckets must not move the headline number."""
        _step("a", read=900, create=100, turns=9, n=1)
        _step("b", read=100, create=900, turns=1, n=1)

        summary = cache_efficiency.cache_summary()

        assert summary["cache_read"] == 1000
        assert summary["cache_creation"] == 1000
