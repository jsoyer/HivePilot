"""Two measurements of the same money, and why they must never be added.

Cost reaches this system by two independent paths:

    envelope -- `steps.cost_usd`, self-reported by the agent in `--print`
                mode's JSON, one figure per step;
    otel     -- `claude_code.cost.usage`, exported per API request.

They measure THE SAME SPEND. Summing them would double-count it, and a
dashboard that shows a single confident total assembled from both is worse than
one that shows neither.

The reason this needs a panel rather than a number is what the box says today:

    envelope  404.51 USD   357 steps    2026-07-26 -> 2026-08-19
    otel      169.13 USD  1929 rows     2026-08-10 -> 2026-08-19

A 2.4x gap that looks like massive telemetry loss and is nothing of the kind:
OTel export only landed on the 10th, so the two cover different windows. Put
those totals side by side without their coverage and somebody spends a day
hunting money that was never missing.

So every basis carries its window, and the report says outright when the two
do not line up.
"""

from __future__ import annotations

from hivepilot.services.cost_basis import compare_cost_bases


def _basis(total=100.0, count=10, first="2026-08-01", last="2026-08-19"):
    return {"total_usd": total, "count": count, "first": first, "last": last}


class TestTheTwoAreNeverAdded:
    def test_no_combined_total_is_offered(self):
        """The single most dangerous thing this could return. Both paths
        measure the same spend, so a sum is double-counting dressed as
        completeness."""
        report = compare_cost_bases(envelope=_basis(), otel=_basis())

        assert "total_usd" not in report
        assert "combined" not in report

    def test_each_basis_keeps_its_own_figure(self):
        report = compare_cost_bases(
            envelope=_basis(total=404.51, count=357), otel=_basis(total=169.13, count=1929)
        )

        assert report["envelope"]["total_usd"] == 404.51
        assert report["otel"]["total_usd"] == 169.13


class TestCoverageIsReportedWithTheNumber:
    def test_different_windows_are_flagged(self):
        """The box's real shape: envelope from 07-26, otel from 08-10. A 2.4x
        gap that is coverage, not loss."""
        report = compare_cost_bases(
            envelope=_basis(first="2026-07-26", last="2026-08-19"),
            otel=_basis(first="2026-08-10", last="2026-08-19"),
        )

        assert report["comparable"] is False

    def test_matching_windows_are_comparable(self):
        report = compare_cost_bases(
            envelope=_basis(first="2026-08-10", last="2026-08-19"),
            otel=_basis(first="2026-08-10", last="2026-08-19"),
        )

        assert report["comparable"] is True

    def test_a_missing_window_is_not_comparable(self):
        """Two totals whose periods nobody can establish cannot be compared,
        and saying they can is how the gap gets misread."""
        report = compare_cost_bases(envelope=_basis(first=None, last=None), otel=_basis())

        assert report["comparable"] is False

    def test_two_unknown_windows_are_not_comparable_either(self):
        """The hole a mutation found. With both windows absent, an equality
        check answers `None == None` -> True and calls them comparable -- two
        totals covering periods nobody can establish, declared equivalent."""
        report = compare_cost_bases(
            envelope=_basis(first=None, last=None), otel=_basis(first=None, last=None)
        )

        assert report["comparable"] is False
        assert report["divergence_pct"] is None


class TestAnAbsentBasisIsNotZero:
    def test_no_otel_rows_reads_as_not_measured(self):
        """Zero dollars is a measurement -- a period in which nothing was
        spent. Absent means the exporter never ran, and collapsing the two is
        how a dead exporter looks like a free week."""
        report = compare_cost_bases(envelope=_basis(), otel=None)

        assert report["otel"] is None
        assert report["comparable"] is False

    def test_no_envelope_rows_reads_as_not_measured(self):
        report = compare_cost_bases(envelope=None, otel=_basis())

        assert report["envelope"] is None

    def test_neither_present_is_not_an_error(self):
        """A fresh deployment has neither, and that is not a fault."""
        report = compare_cost_bases(envelope=None, otel=None)

        assert report["envelope"] is None
        assert report["otel"] is None
        assert report["comparable"] is False


class TestTheDivergence:
    def test_it_is_reported_only_when_the_windows_match(self):
        """A ratio across different periods is a number that means nothing,
        and printing it invites exactly the wrong conclusion."""
        report = compare_cost_bases(
            envelope=_basis(total=404.51, first="2026-07-26"),
            otel=_basis(total=169.13, first="2026-08-10"),
        )

        assert report["divergence_pct"] is None

    def test_a_real_divergence_is_named(self):
        report = compare_cost_bases(envelope=_basis(total=100.0), otel=_basis(total=80.0))

        assert report["divergence_pct"] == 20.0

    def test_agreement_is_zero_not_none(self):
        """Zero divergence is a finding: the two paths agree. `None` means the
        question could not be asked."""
        report = compare_cost_bases(envelope=_basis(total=100.0), otel=_basis(total=100.0))

        assert report["divergence_pct"] == 0.0

    def test_a_zero_envelope_does_not_divide_by_zero(self):
        report = compare_cost_bases(envelope=_basis(total=0.0), otel=_basis(total=0.0))

        assert report["divergence_pct"] is None
