"""A cut nobody records is the reason this took a day to find.

Run 639 discarded roughly 67 000 of its own 75 728 characters before the
release gate ever read them, and said nothing at all. Not a warning, not a
counter, not a line in the log. The only visible consequence was a gate
blocking for a "missing" clearance that had in fact been given, which reads
as an agent problem and sent me looking in entirely the wrong place.

Raising the budget was necessary and is not sufficient: it moves the number,
it does not make the decision observable. A budget is a guess about a model
whose real window the engine never consults, so the guess WILL be wrong for
some deployment. When it is wrong, that must be legible from the log rather
than inferred a day later from an offset in a database column.

So truncation now states what it dropped, per stage, with the totals that
justify tuning the budget on evidence instead of taste.
"""

from __future__ import annotations

import pytest

from hivepilot import orchestrator


@pytest.fixture
def events(monkeypatch):
    captured: list[tuple[str, dict]] = []

    class Logger:
        def warning(self, event, **fields):
            captured.append((event, fields))

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(orchestrator, "logger", Logger())
    return captured


class TestTruncationAnnouncesItself:
    def test_a_cut_is_reported_with_its_numbers(self, events):
        chunks = ["## A\n" + "x" * 40_000, "## B\n" + "y" * 40_000]

        orchestrator.build_prior_context(chunks, mode="cap", max_chars=1_000)

        assert events, "the context was cut and nothing said so"
        event, fields = events[0]
        assert event == "context.truncated"
        assert fields["total_chars"] > 79_000
        assert fields["budget"] == 1_000
        assert fields["dropped_chars"] > 78_000
        assert fields["stages"] == 2

    def test_content_that_fits_says_nothing(self, events):
        """Silence is correct here -- nothing was lost."""
        orchestrator.build_prior_context(["## A\nshort"], mode="cap", max_chars=8_000)

        assert events == []

    def test_the_other_modes_do_not_report_a_cut(self, events):
        """`full` never cuts. `synthesis` drops chunks by design and by name,
        which is a different decision with its own contract -- conflating the
        two would make the counter meaningless."""
        chunks = ["## Plan Synthesis\np", "## Middle\nm", "## Last\nl"]

        orchestrator.build_prior_context(chunks, mode="full", max_chars=1)
        orchestrator.build_prior_context(chunks, mode="synthesis", max_chars=1)

        assert events == []

    def test_the_report_survives_a_single_runaway_stage(self, events):
        orchestrator.build_prior_context(
            ["## Runaway\n" + "x" * 500_000], mode="cap", max_chars=100
        )

        event, fields = events[0]
        assert event == "context.truncated"
        assert fields["stages"] == 1
        assert fields["dropped_chars"] > 499_000
