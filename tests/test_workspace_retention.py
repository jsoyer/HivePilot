"""Keep a failed run's workspace open, but not forever.

A run that succeeded leaves nothing worth opening. A run that FAILED is the one
an operator actually wants to look at -- the agent's scrollback, its exact
environment, the ability to re-run the command that failed where it failed.
Closing those was the wrong default, and I defended it with an accumulation
cost I had asserted rather than measured.

Measured afterwards, on the box: 7-10 failed runs a day on busy days, 233 in a
month. So the cost is real -- which argues for a BOUND, not for closing. At
N=5 that leaves about half a day of failures open, which is the window in which
anyone actually looks.

What a commit cannot replace, and why this exists at all: committing
work-in-progress on failure preserves FILES. It does not preserve the terminal,
the environment, or the ability to poke at the tree in place. The two are
complements, not alternatives.

The retention rule is a pure function because it decides what gets destroyed,
and that is not a decision anybody should have to trace through a call graph.
"""

from __future__ import annotations

import pytest

from hivepilot.services.workspace_retention import run_id_from_label, workspaces_to_close


class TestTheBound:
    def test_nothing_is_closed_below_the_bound(self):
        kept = [("w1", 101), ("w2", 102)]

        assert workspaces_to_close(kept, keep=5) == []

    def test_the_oldest_beyond_the_bound_are_closed(self):
        kept = [("w1", 101), ("w2", 102), ("w3", 103)]

        assert workspaces_to_close(kept, keep=2) == ["w1"]

    def test_the_newest_are_the_ones_retained(self):
        """Ordered by run id, not by discovery order: `workspace list` returns
        whatever herdr feels like, and closing by that would keep an arbitrary
        set."""
        kept = [("w_old", 10), ("w_new", 99), ("w_mid", 50)]

        assert workspaces_to_close(kept, keep=1) == ["w_old", "w_mid"]

    def test_a_bound_of_zero_closes_everything(self):
        """`keep_failed_runs: 0` is the default and must mean today's
        behaviour: nothing is retained."""
        kept = [("w1", 1), ("w2", 2)]

        assert set(workspaces_to_close(kept, keep=0)) == {"w1", "w2"}

    @pytest.mark.parametrize("keep", [-1, -100])
    def test_a_negative_bound_is_not_read_as_unlimited(self, keep):
        """A config typo must not silently disable the bound. Negative means
        'retain none', never 'retain all'."""
        kept = [("w1", 1), ("w2", 2)]

        assert set(workspaces_to_close(kept, keep=keep)) == {"w1", "w2"}

    def test_nothing_kept_closes_nothing(self):
        assert workspaces_to_close([], keep=3) == []


class TestItOnlyManagesItsOwn:
    """The pruner closes things. Anything it cannot positively identify as a
    HivePilot run workspace is left alone -- an operator's own workspace, or
    one from another tool, must never be closed by a bound it never opted
    into."""

    def test_a_label_it_minted_yields_its_run_id(self):
        assert run_id_from_label("forage-gate-probe run 696") == 696

    def test_a_label_from_anything_else_is_not_ours(self):
        assert run_id_from_label("hivepilot") is None
        assert run_id_from_label("my scratch pad") is None
        assert run_id_from_label("") is None

    def test_a_run_word_without_a_number_is_not_ours(self):
        assert run_id_from_label("noxys run later") is None

    def test_a_project_named_run_does_not_confuse_it(self):
        """The id is the LAST token, and it must be a number."""
        assert run_id_from_label("run run 42") == 42

    def test_a_trailing_number_that_is_not_a_run_id_is_refused(self):
        """Without the `run` marker this is somebody else's label that happens
        to end in a digit."""
        assert run_id_from_label("sprint 3") is None
