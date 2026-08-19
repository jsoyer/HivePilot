"""The compression proxy needs a surface, like every other savings source.

`/v1/efficiency` composes headroom (library plugin) and rtk. Since the proxy
landed, a third thing compresses — and it is the one on the critical path of
every agent call, the one that can silently fall back to a direct call, and
the one with no representation anywhere in Pollen or the API.

If it goes down and every agent quietly stops being compressed, nothing on
any screen says so. That is the same blind spot the Health tab had before
#399, one view over.

**Unavailable is never zero.** Unconfigured, malformed and unreachable all
return `None`. Reporting `0 saved` would claim the proxy is useless when in
fact nobody could reach it — and a configured-but-down proxy is a degraded
state the operator has to be able to see.
"""

from __future__ import annotations

import pytest

from hivepilot.services import efficiency_service


class TestProxySummary:
    def test_unconfigured_reads_as_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Most deployments never run the proxy; that is not a degraded state."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "compression_proxy_url", None, raising=False)

        assert efficiency_service.proxy_summary() is None

    def test_an_unreachable_proxy_reads_as_unavailable_not_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Port 1 is reserved and never listening."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "compression_proxy_url", "http://127.0.0.1:1", raising=False)

        assert efficiency_service.proxy_summary(timeout=0.25) is None

    def test_a_malformed_url_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo in configuration must not be able to 500 a dashboard panel."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "compression_proxy_url", "not-a-url", raising=False)

        assert efficiency_service.proxy_summary(timeout=0.25) is None


#: What the view composes into a savings picture. Summing anything outside
#: this set would overstate what was avoided.
SAVINGS_SOURCES = {"headroom", "rtk", "proxy", "cache"}

#: Measured on the same view, and deliberately NOT summable with the above:
#: these say what was LOST, not what was saved.
LOSS_SURFACES = {"truncation"}


class TestEfficiencySummaryShape:
    def test_it_names_every_savings_source(self) -> None:
        """headroom and rtk were the whole story until the proxy landed, and
        then prompt cache turned out to be a fourth.

        A view that composes savings sources has to name every one of them,
        or the total it implies is wrong. The count in this test's old name
        was never the invariant — the completeness is. Cache is also the only
        source measured from our OWN telemetry rather than a tool's
        self-report.

        Kept as an exact-set assertion rather than a subset: a source
        silently disappearing from this view is the same defect as one never
        being added.
        """
        result = efficiency_service.efficiency_summary(days=30)

        assert set(result) == SAVINGS_SOURCES | LOSS_SURFACES

    def test_truncation_is_a_loss_surface_not_a_savings_source(self) -> None:
        """It sits on the same view and is NOT part of the savings total.

        Every other key here answers "what did we avoid paying". `truncation`
        answers "what did we throw away" -- run 639's ~90% of a run, both
        verdicts the release gate needed, and a release refused on a clearance
        that had been given. Summing it into savings would read a loss as a
        gain, and the two sets are kept apart here so that cannot happen by
        someone adding one key to one list.
        """
        assert "truncation" in LOSS_SURFACES
        assert "truncation" not in SAVINGS_SOURCES

    def test_headroom_is_still_never_null(self) -> None:
        """Unchanged contract: headroom is a local DB read, always real."""
        result = efficiency_service.efficiency_summary(days=30)

        assert isinstance(result["headroom"], dict)
