"""Truncation was only ever logged, so nobody could see it happening.

Run 639 is why this exists. `cap` mode kept the TAIL of the joined prior
context, so ~90% of the run vanished — including both verdicts the release gate
needed — and the gate then refused a release on a clearance that HAD been
given. It took a week to find, because the only trace was a `logger.warning`
in a file nobody opens until something is already wrong.

The warning has carried the numbers that explain it since that fix:
`total_chars`, `budget`, `dropped_chars`, `stages`, `largest_stage_chars`. What
it never carried is the budget's BASIS — `derived` from the model's window, or
`fallback` to a configured constant — and that is the field that says whether
the number is a measurement or a guess.

Persisting it turns a forensic dig into a surface. The summary below is what a
dashboard needs to answer three questions: is this happening, how badly, and to
which stage.

The counting rule that matters, and it is the same one as everywhere else
today: an empty table reads as "nothing has been RECORDED", never as "nothing
was truncated". The two differ — the second is a fact about runs, the first is
a fact about whether anyone was writing it down, and confusing them is the
house defect.
"""

from __future__ import annotations

from hivepilot.services.truncation_report import summarise_truncations


def _event(**kw):
    base = {
        "project": "noxys",
        "role": "reviewer",
        "total_chars": 100_000,
        "budget": 40_000,
        "dropped_chars": 60_000,
        "stages": 8,
        "largest_stage_chars": 55_000,
        "budget_basis": "derived",
    }
    base.update(kw)
    return base


class TestTheSummary:
    def test_no_rows_says_nothing_was_recorded(self):
        """NOT "nothing was truncated". A dashboard that shows a confident
        zero for a table nobody writes to is the exact shape of run 639."""
        summary = summarise_truncations([])

        assert summary["recorded"] == 0
        assert summary["dropped_chars"] == 0
        assert summary["worst_stage_chars"] is None

    def test_it_counts_events_and_totals_what_was_lost(self):
        summary = summarise_truncations([_event(), _event(dropped_chars=10_000)])

        assert summary["recorded"] == 2
        assert summary["dropped_chars"] == 70_000

    def test_the_worst_single_stage_is_reported_not_the_average(self):
        """An average hides the case that matters. The point of this figure is
        to name the ONE stage whose output is blowing the budget."""
        # BIGGEST FIRST, deliberately. With the larger value last, "keep the
        # maximum" and "keep the most recent" give the same answer and the test
        # proves neither -- a mutation replacing max() with plain assignment
        # passed until this order was fixed.
        summary = summarise_truncations(
            [_event(largest_stage_chars=90_000), _event(largest_stage_chars=10_000)]
        )

        assert summary["worst_stage_chars"] == 90_000

    def test_the_basis_split_is_reported(self):
        """`derived` means the budget came from the model's real window;
        `fallback` means it came from a constant nobody re-checked. A total
        that mixes the two without saying so is an aggregate no one can act
        on."""
        summary = summarise_truncations(
            [
                _event(budget_basis="derived"),
                _event(budget_basis="fallback"),
                _event(budget_basis="fallback"),
            ]
        )

        assert summary["by_basis"] == {"derived": 1, "fallback": 2}

    def test_an_unknown_basis_is_kept_as_unknown_not_folded_in(self):
        """An old row written before the basis was recorded is not a
        `fallback` — it is a row we cannot classify, and saying so is the
        difference between a gap and a guess."""
        summary = summarise_truncations([_event(budget_basis=None)])

        assert summary["by_basis"] == {"unknown": 1}

    def test_the_worst_offending_role_is_named(self):
        """So the operator knows whose output to look at, rather than that
        'something' is too long."""
        summary = summarise_truncations(
            [
                _event(role="reviewer", dropped_chars=5_000),
                _event(role="ciso", dropped_chars=50_000),
                _event(role="ciso", dropped_chars=1_000),
            ]
        )

        assert summary["worst_role"] == "ciso"

    def test_a_row_with_no_role_does_not_win_by_default(self):
        """A NULL role sorts as something; it must not become the answer to
        'who should I go look at'."""
        summary = summarise_truncations(
            [_event(role=None, dropped_chars=99_000), _event(role="qa", dropped_chars=1_000)]
        )

        assert summary["worst_role"] == "qa"

    def test_a_missing_dropped_count_is_skipped_rather_than_read_as_zero(self):
        """Zero dropped characters is a measurement. Absent is not, and adding
        it in as zero would understate the total."""
        summary = summarise_truncations([_event(dropped_chars=None), _event()])

        assert summary["recorded"] == 2
        assert summary["dropped_chars"] == 60_000

    def test_a_missing_stage_size_does_not_become_the_worst(self):
        """Same rule, other field: an unwritten largest-stage must not compete
        with a real one, in either direction."""
        summary = summarise_truncations(
            [_event(largest_stage_chars=None), _event(largest_stage_chars=42_000)]
        )

        assert summary["worst_stage_chars"] == 42_000

    def test_every_stage_size_missing_leaves_it_unknown(self):
        summary = summarise_truncations([_event(largest_stage_chars=None)])

        assert summary["worst_stage_chars"] is None


class TestTheCallbackIsHowItGetsRecorded:
    """`build_prior_context` stays pure: it takes a callback and knows nothing
    about a database. The caller is the only place that knows the run, the
    project, the role and the budget's basis."""

    def test_no_truncation_means_no_callback(self):
        """The discriminating case. A run that fitted its budget must not
        record a truncation of zero characters -- that is a row saying
        something happened when nothing did."""
        from hivepilot.orchestrator import build_prior_context

        seen = []
        build_prior_context(["short"], mode="cap", max_chars=10_000, on_truncate=seen.append)

        assert seen == []

    def test_truncation_hands_over_the_numbers(self):
        from hivepilot.orchestrator import build_prior_context

        seen = []
        build_prior_context(
            ["x" * 5_000, "y" * 5_000], mode="cap", max_chars=1_000, on_truncate=seen.append
        )

        assert len(seen) == 1
        facts = seen[0]
        assert facts["dropped_chars"] > 0
        assert facts["budget"] == 1_000
        assert facts["stages"] == 2
        assert facts["largest_stage_chars"] == 5_000

    def test_a_failing_recorder_does_not_fail_the_step(self):
        """Truncation is already a degradation. Losing the run because we
        could not write it down would be a second, worse one."""
        from hivepilot.orchestrator import build_prior_context

        def explode(_facts):
            raise RuntimeError("database is on fire")

        result = build_prior_context(
            ["x" * 5_000], mode="cap", max_chars=1_000, on_truncate=explode
        )

        assert result is not None

    def test_full_mode_never_records(self):
        """`full` does not truncate, so there is nothing to report -- and a
        row here would misattribute a budget that was never applied."""
        from hivepilot.orchestrator import build_prior_context

        seen = []
        build_prior_context(["x" * 99_000], mode="full", max_chars=10, on_truncate=seen.append)

        assert seen == []
