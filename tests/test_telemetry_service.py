"""Tests for OTLP telemetry ingest.

The fixture is a **real** payload, captured from `claude --print` on the
production host and redacted only in its identity values. Shapes, arms and
attribute names are untouched.

That matters here more than usual: a filter written in this project once
passed its own invented fixture and then saved 0% on all three real inputs,
because the fixture and the code came from the same wrong model of the data.
If the wire format drifts, `test_fixture_is_the_real_shape` fails and says so
rather than the parser silently returning nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "otlp_claude_code_metrics.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_fixture_is_the_real_shape(payload):
    """Pin what was actually observed, so drift is loud."""
    scope = payload["resourceMetrics"][0]["scopeMetrics"][0]
    assert scope["scope"]["name"] == "com.anthropic.claude_code"

    names = {m["name"] for m in scope["metrics"]}
    assert {
        "claude_code.token.usage",
        "claude_code.cost.usage",
        "claude_code.session.count",
        "claude_code.active_time.total",
    } <= names

    # Token usage is split by `type`; the cache arms are the ones we came for.
    token = next(m for m in scope["metrics"] if m["name"] == "claude_code.token.usage")
    kinds = {
        a["value"]["stringValue"]
        for p in token["sum"]["dataPoints"]
        for a in p["attributes"]
        if a["key"] == "type"
    }
    assert {"input", "output", "cacheRead", "cacheCreation"} <= kinds


def test_parses_every_data_point(payload):
    from hivepilot.services.telemetry_service import parse_otlp_metrics

    points = parse_otlp_metrics(payload)

    assert points, "a real payload must not parse to nothing"
    tokens = [p for p in points if p.metric == "claude_code.token.usage"]
    assert {p.kind for p in tokens} == {"input", "output", "cacheRead", "cacheCreation"}
    assert all(p.model for p in tokens), "model must survive parsing"
    assert any(p.metric == "claude_code.cost.usage" and p.value > 0 for p in points)


def test_identity_never_survives_ingest(payload):
    """Dropped on the way in, not filtered on read.

    A value that is never stored cannot leak out of a query written later, and
    none of it is needed to answer what a call cost.
    """
    from hivepilot.services.telemetry_service import IDENTITY_ATTRS, parse_otlp_metrics

    points = parse_otlp_metrics(payload)

    for point in points:
        leaked = IDENTITY_ATTRS & set(point.attributes)
        assert not leaked, f"identity survived: {leaked}"
        assert "@" not in json.dumps(point.attributes), "an email-shaped value survived"

    # session.id is deliberately KEPT: it correlates points, and identifies nobody.
    assert any(p.session_id for p in points)


def test_unknown_metric_kind_is_skipped_not_fatal():
    """Schema drift in a dependency must not become noise on the agent.

    Returning an error would make the exporter retry and log against the very
    thing being measured.
    """
    from hivepilot.services.telemetry_service import parse_otlp_metrics

    weird = {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {"name": "future.metric", "somethingNew": {"dataPoints": []}},
                            {"name": "no.name.arm"},
                            {
                                "name": "claude_code.cost.usage",
                                "sum": {"dataPoints": [{"asDouble": 1.5, "attributes": []}]},
                            },
                        ]
                    }
                ]
            }
        ]
    }

    points = parse_otlp_metrics(weird)

    assert [p.metric for p in points] == ["claude_code.cost.usage"]
    assert points[0].value == 1.5


def test_int_valued_points_are_read():
    """OTLP may carry asInt instead of asDouble; both are values."""
    from hivepilot.services.telemetry_service import parse_otlp_metrics

    points = parse_otlp_metrics(
        {
            "resourceMetrics": [
                {
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {"dataPoints": [{"asInt": "42", "attributes": []}]},
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )

    assert points[0].value == 42.0


# ---------------------------------------------------------------------------
# Cache economics
# ---------------------------------------------------------------------------


def test_amortisation_below_one_is_a_loss():
    from hivepilot.services.telemetry_service import SessionCache

    losing = SessionCache("s1", "opus", created=1000, read=100)
    winning = SessionCache("s2", "opus", created=1000, read=16000)

    assert losing.amortisation == pytest.approx(0.1)
    assert losing.wasted_tokens == 900
    assert winning.amortisation == pytest.approx(16.0)
    assert winning.wasted_tokens == 0


def test_report_uses_the_median_not_the_total():
    """The whole point of the check.

    One session reading 100x hides nineteen that read nothing when you sum.
    An 85% fleet hit rate coexisted here with 1.7M tokens of wasted creation
    for exactly this reason, and a detector that sums would have found none of
    it.
    """
    from hivepilot.services import telemetry_service

    sessions = [telemetry_service.SessionCache(f"s{i}", "opus", 1000, 0) for i in range(19)]
    sessions.append(telemetry_service.SessionCache("whale", "opus", 1000, 100_000))

    report = telemetry_service.CacheReport(
        sessions=len(sessions),
        median_amortisation=telemetry_service.median([s.amortisation for s in sessions]),
        below_one=len([s for s in sessions if s.amortisation < 1.0]),
        worst=min(sessions, key=lambda s: s.amortisation),
        wasted_tokens=sum(s.wasted_tokens for s in sessions if s.amortisation < 1.0),
    )

    # Summing would give 100000/20000 = 5.0 and call this healthy.
    assert report.median_amortisation == 0.0
    assert report.below_one == 19
    assert not report.healthy
    assert report.wasted_tokens == 19_000


def test_empty_report_is_not_a_failure():
    from hivepilot.services.telemetry_service import CacheReport

    empty = CacheReport(0, 0.0, 0, None, 0.0)
    assert empty.healthy
    assert empty.worst is None
