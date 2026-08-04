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


class TestEfficiencySummaryShape:
    def test_it_now_carries_three_sources(self) -> None:
        """headroom and rtk were the whole story until the proxy landed.

        A view that composes savings sources has to name every one of them,
        or the total it implies is wrong.
        """
        result = efficiency_service.efficiency_summary(days=30)

        assert set(result) == {"headroom", "rtk", "proxy"}

    def test_headroom_is_still_never_null(self) -> None:
        """Unchanged contract: headroom is a local DB read, always real."""
        result = efficiency_service.efficiency_summary(days=30)

        assert isinstance(result["headroom"], dict)
