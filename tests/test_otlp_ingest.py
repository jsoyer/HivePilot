"""Tests for the OTLP metric ingest route.

The route is unauthenticated by design, which makes its two guards the whole
security story: it must be off unless someone turned it on, and it must refuse
anything that is not loopback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "otlp_claude_code_metrics.json"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "state.db"))
    from hivepilot.services import api_service

    return TestClient(api_service.app)


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_disabled_by_default(client, payload, monkeypatch):
    """An ingest route nobody asked for is a write endpoint nobody watches."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "otel_ingest_enabled", False, raising=False)

    resp = client.post("/otlp/v1/metrics", json=payload)

    assert resp.status_code == 404


def test_accepts_a_real_payload_when_enabled(client, payload, monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "otel_ingest_enabled", True, raising=False)

    resp = client.post("/otlp/v1/metrics", json=payload)

    assert resp.status_code == 200
    # OTLP expects an ExportMetricsServiceResponse; an empty partialSuccess
    # means "all of it was accepted".
    assert resp.json() == {"partialSuccess": {}}


def test_rejects_non_loopback(client, payload, monkeypatch):
    """The only thing standing in for authentication."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "otel_ingest_enabled", True, raising=False)

    resp = client.post(
        "/otlp/v1/metrics",
        json=payload,
        headers={"X-Forwarded-For": "203.0.113.9"},
    )

    assert resp.status_code == 403


def test_malformed_body_does_not_500(client, monkeypatch):
    """An error response makes the exporter retry and log against the agent.

    Whatever arrives, this route answers 200 and drops what it cannot read.
    """
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "otel_ingest_enabled", True, raising=False)

    for body in ({}, {"resourceMetrics": None}, {"resourceMetrics": [{"junk": 1}]}):
        resp = client.post("/otlp/v1/metrics", json=body)
        assert resp.status_code == 200, body


def test_ingested_points_are_queryable(client, payload, monkeypatch):
    from hivepilot.config import settings
    from hivepilot.services import telemetry_service

    monkeypatch.setattr(settings, "otel_ingest_enabled", True, raising=False)

    client.post("/otlp/v1/metrics", json=payload)
    report = telemetry_service.cache_report()

    assert report.sessions >= 1
    # The captured call created 3110 tokens of cache and read back 18356.
    assert report.median_amortisation > 1.0


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def test_cache_endpoint_requires_a_token(client):
    assert client.get("/v1/telemetry/cache").status_code == 401


def test_cache_endpoint_reports_median_and_losers(client, payload, monkeypatch, tmp_path):
    """The screen has to show the count below break-even, not an average.

    A fleet ratio is dominated by whichever session read the most, so the
    endpoint exposes the same shape the doctor check does.
    """
    from hivepilot.config import settings
    from hivepilot.services.token_service import add_token

    monkeypatch.setattr(settings, "tokens_file", tmp_path / "tokens.json")
    monkeypatch.setattr(settings, "otel_ingest_enabled", True, raising=False)
    client.post("/otlp/v1/metrics", json=payload)
    raw, _ = add_token("read")

    resp = client.get("/v1/telemetry/cache", headers=_auth(raw))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"sessions", "median_amortisation", "below_one", "wasted_tokens", "worst"}
    assert body["sessions"] >= 1


def test_cache_endpoint_is_empty_not_broken_without_telemetry(client, monkeypatch, tmp_path):
    """Ingest is opt-in; nothing recorded is a valid, reportable state."""
    from hivepilot.config import settings
    from hivepilot.services.token_service import add_token

    monkeypatch.setattr(settings, "tokens_file", tmp_path / "tokens.json")
    raw, _ = add_token("read")

    resp = client.get("/v1/telemetry/cache", headers=_auth(raw))

    assert resp.status_code == 200
    assert resp.json()["sessions"] == 0
    assert resp.json()["worst"] is None
