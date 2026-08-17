"""Scanning a quarter of the vault must not look like scanning the vault.

Measured on the box, 2026-08-17: `noxys-obsidian-vault` holds **2 125** notes
and `_MAX_NOTES_SCANNED` is **500**. Recall therefore sees 24% of the corpus
and says nothing about the other 76%.

The cap already caused one real incident. It used to take
`sorted(rglob("*.md"))[:500]` -- ALPHABETICAL -- so the 500 it read were 24%
of the vault chosen by filename, and every note the agents had produced was
structurally excluded. The fix changed the KEY to mtime. It did not touch the
principle: a note of substance written six months ago is still out of reach of
recall, and nothing anywhere says so.

That is the same defect as the context truncation of run 639, in a different
subsystem: a cut that reports nothing is indistinguishable from no cut at all.
An operator asking "why did it not remember the ADR" has no way to learn that
the ADR was never eligible.

So the scan reports what it left out, with the numbers that make the cap
tunable on evidence: how many notes exist, how many were read, how many were
skipped. Silence stays correct when nothing was skipped.
"""

from __future__ import annotations

import pytest

from hivepilot.services.vault_scan import ScanBudget, plan_scan


class TestTheBudgetReportsWhatItSkipped:
    def test_the_measured_noxys_shape(self):
        """2 125 notes against a 500 cap: 1 625 unreachable."""
        budget = plan_scan(total=2_125, cap=500)

        assert budget.scanned == 500
        assert budget.skipped == 1_625
        assert budget.truncated is True

    def test_a_corpus_under_the_cap_is_read_whole(self):
        budget = plan_scan(total=120, cap=500)

        assert budget.scanned == 120
        assert budget.skipped == 0
        assert budget.truncated is False

    def test_a_corpus_exactly_at_the_cap_is_not_truncated(self):
        """The off-by-one that would report a phantom cut on a full read."""
        budget = plan_scan(total=500, cap=500)

        assert budget.truncated is False
        assert budget.skipped == 0

    def test_an_empty_vault_is_not_an_error(self):
        budget = plan_scan(total=0, cap=500)

        assert budget.scanned == 0
        assert budget.truncated is False


class TestTheCoverageIsExpressedAsAFraction:
    def test_coverage_is_reported_so_the_cap_is_tunable(self):
        """A raw 'skipped 1625' invites a guess. A fraction says how bad it
        is at a glance -- and 0.24 is the number that made this worth fixing."""
        budget = plan_scan(total=2_125, cap=500)

        assert budget.coverage == pytest.approx(0.235, abs=0.01)

    def test_a_full_read_is_full_coverage(self):
        assert plan_scan(total=10, cap=500).coverage == 1.0

    def test_an_empty_vault_reports_full_coverage_not_a_division_by_zero(self):
        assert plan_scan(total=0, cap=500).coverage == 1.0


class TestGuardsOnTheCapItself:
    @pytest.mark.parametrize("cap", [0, -1])
    def test_a_non_positive_cap_reads_nothing_and_says_so(self, cap):
        """A cap of zero is a configuration mistake, not 'read everything'.
        It must be visible, not silently generous."""
        budget = plan_scan(total=100, cap=cap)

        assert budget.scanned == 0
        assert budget.truncated is True
        assert budget.skipped == 100

    def test_the_budget_is_frozen(self):
        """A caller must not be able to rewrite the numbers it reports."""
        budget = plan_scan(total=2_125, cap=500)

        with pytest.raises(Exception):
            budget.scanned = 9_999  # type: ignore[misc]

    def test_it_is_a_scan_budget(self):
        assert isinstance(plan_scan(total=1, cap=1), ScanBudget)
