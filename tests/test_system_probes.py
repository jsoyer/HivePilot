"""Two probes for the things that fail by going quiet.

Both answer a question the rest of the dashboard cannot, because both fail in
the same way: they stop producing, and an absence looks exactly like a healthy
zero.

    the agent surface -- is a backend configured at all, and if one is, does it
    answer? A view that renders every role as idle because it cannot reach the
    server is worse than one that admits it cannot reach it.

    OTel export -- is telemetry still ARRIVING? An exporter that quietly
    stopped leaves a row count that looks fine and a newest row from last
    Tuesday. That is the plausible zero somebody spends a day chasing.

Three states each, and the middle one is the point:

    not configured   -- nothing is wrong; nobody asked for this
    configured, dead -- something IS wrong, and it is actionable
    working          -- with the evidence that says so

Collapsing the first two into "unhealthy" cries wolf on every deployment that
simply does not use the feature. Collapsing them into "ok" hides the outage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hivepilot.services.system_probes import probe_agent_surface, probe_otel_arrival


class TestTheAgentSurfaceProbe:
    def test_no_backend_configured_is_not_a_failure(self):
        """Empty is the default and the honest one. Reporting it as unhealthy
        would put a red badge on every deployment that never asked for a live
        agent surface."""
        result = probe_agent_surface(backend="", run_cli=lambda argv: 0)

        assert result["state"] == "not_configured"

    def test_a_configured_backend_that_answers_is_working(self):
        result = probe_agent_surface(backend="herdr", run_cli=lambda argv: 0)

        assert result["state"] == "ok"
        assert result["backend"] == "herdr"

    def test_a_configured_backend_that_does_not_answer_is_a_failure(self):
        """The state worth having. Something was asked for and is not there."""
        result = probe_agent_surface(backend="herdr", run_cli=lambda argv: 1)

        assert result["state"] == "unreachable"

    def test_a_probe_that_raises_is_unreachable_not_ok(self):
        """A missing binary raises rather than returning non-zero, and reading
        that as healthy is how a dead surface looks alive."""

        def explode(argv):
            raise FileNotFoundError("no such binary")

        assert probe_agent_surface(backend="orca", run_cli=explode)["state"] == "unreachable"

    def test_an_unknown_backend_name_is_refused_rather_than_probed(self):
        """A typo in configuration must not become a shell command."""
        result = probe_agent_surface(backend="; rm -rf /", run_cli=lambda argv: 0)

        assert result["state"] == "unknown_backend"


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%d %H:%M:%S")


class TestTheOtelArrivalProbe:
    def test_no_rows_ever_is_reported_as_never_arrived(self):
        """NOT "stale". Nothing has ever come, which points at configuration
        rather than at an exporter that stopped."""
        result = probe_otel_arrival(rows=0, newest=None, stale_after_hours=6)

        assert result["state"] == "never_arrived"

    def test_recent_rows_are_ok(self):
        result = probe_otel_arrival(
            rows=10_882, newest=_iso(timedelta(minutes=5)), stale_after_hours=6
        )

        assert result["state"] == "ok"
        assert result["rows"] == 10_882

    def test_rows_that_stopped_coming_are_stale_not_ok(self):
        """The whole reason this exists. The count still looks healthy; it is
        the AGE that says the exporter died."""
        result = probe_otel_arrival(
            rows=10_882, newest=_iso(timedelta(days=3)), stale_after_hours=6
        )

        assert result["state"] == "stale"

    def test_an_unparseable_timestamp_is_unknown_not_ok(self):
        """A row we cannot date cannot attest freshness, and reading it as
        fresh is the failure this probe exists to catch."""
        result = probe_otel_arrival(rows=5, newest="not-a-date", stale_after_hours=6)

        assert result["state"] == "unknown"

    def test_rows_present_but_no_timestamp_is_unknown(self):
        result = probe_otel_arrival(rows=5, newest=None, stale_after_hours=6)

        assert result["state"] == "unknown"

    @pytest.mark.parametrize("hours", [0, -1])
    def test_a_nonsense_threshold_does_not_make_everything_fresh(self, hours):
        """A zero or negative window would mark every row stale, or every row
        fresh, depending on the comparison. Either way the probe stops
        measuring, so the threshold is floored instead."""
        result = probe_otel_arrival(
            rows=5, newest=_iso(timedelta(minutes=1)), stale_after_hours=hours
        )

        assert result["state"] == "ok"
