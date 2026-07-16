"""Tests for api_service: /healthz, /readyz, /metrics endpoints.

More comprehensive observability tests live in test_observability.py.
This file exists so the TDD hook allows editing api_service.py.

The analytics endpoint tests (Phase 24a) live in this file too — they mirror
the auth/tenant-isolation patterns established in test_multi_tenant.py.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token


def test_healthz_ok():

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_v1_healthz_ok():

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/v1/healthz")
    assert resp.status_code == 200


def test_readyz_shape():

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "checks" in data


def test_metrics_content_type():

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_metrics_no_local_registry():
    """api_service must not define its own CollectorRegistry — uses shared one."""
    from pathlib import Path

    from hivepilot.services import api_service

    source = Path(api_service.__file__).read_text()
    assert "CollectorRegistry()" not in source


def test_no_run_counter_in_api_service():
    """run_counter was removed; only complete_run increments runs_total."""
    from pathlib import Path

    from hivepilot.services import api_service

    source = Path(api_service.__file__).read_text()
    assert "run_counter" not in source


# ---------------------------------------------------------------------------
# Analytics endpoints (Phase 24a)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


class TestAnalyticsAuth:
    def test_summary_requires_auth(self, api_client):
        resp = api_client.get("/v1/analytics/summary")
        assert resp.status_code == 401

    def test_summary_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        """A token whose role isn't in ROLE_RANKS resolves to rank -1, below
        the 'read' floor required by analytics endpoints -> 403."""
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/analytics/summary", headers=_auth(raw))
        assert resp.status_code == 403

    def test_summary_allows_read_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary", headers=_auth(raw))
        assert resp.status_code == 200


class TestAnalyticsTenantIsolation:
    def test_summary_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success", tenant="acme")
        state_service.record_run_start("p", "t", status="success", tenant="other")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/summary", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_summary_admin_sees_all_tenants(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success", tenant="acme")
        state_service.record_run_start("p", "t", status="success", tenant="other")

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/analytics/summary", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_durations_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success", tenant="acme")
        state_service.record_run_start("p", "t", status="success", tenant="other")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/durations", headers=_auth(raw))
        assert resp.status_code == 200

    def test_step_failures_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(run_acme, "deploy", "failed")
        state_service.record_step(run_other, "deploy", "failed")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/steps/failures", headers=_auth(raw))
        assert resp.status_code == 200
        total = sum(h["count"] for h in resp.json()["hotspots"])
        assert total == 1

    def test_approvals_latency_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", tenant="acme")
        run_other = state_service.record_run_start("p", "t", tenant="other")
        state_service.record_approval_request(run_acme, "p", "t", {}, tenant="acme")
        state_service.record_approval_request(run_other, "p", "t", {}, tenant="other")
        state_service.update_approval(run_acme, "approved")
        state_service.update_approval(run_other, "approved")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/approvals/latency", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


class TestAnalyticsEndpointShapes:
    def test_trends_default_bucket_is_day(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/trends", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["bucket"] == "day"
        assert "series" in data

    def test_trends_week_bucket(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/trends?bucket=week", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["bucket"] == "week"

    def test_trends_invalid_bucket_returns_400(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/trends?bucket=month", headers=_auth(raw))
        assert resp.status_code == 400

    def test_durations_shape(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/durations", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert "overall" in data
        assert "p50" in data["overall"]

    def test_steps_failures_shape(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/steps/failures", headers=_auth(raw))
        assert resp.status_code == 200
        assert "hotspots" in resp.json()

    def test_approvals_latency_shape(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/approvals/latency", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert "p50" in data
        assert "p95" in data

    def test_unversioned_routes_also_registered(self, api_client, tmp_tokens_file):
        """api_service dual-registers unversioned + /v1 routes (matches GET /runs)."""
        raw, _ = add_token("read")
        resp = api_client.get("/analytics/summary", headers=_auth(raw))
        assert resp.status_code == 200


class TestAnalyticsCsvExport:
    def test_summary_csv(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        rows = resp.text.strip().splitlines()
        assert len(rows) >= 2  # header + at least one data row

    def test_trends_csv(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/trends?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_durations_csv(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/durations?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_steps_failures_csv(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "deploy", "failed")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/steps/failures?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        rows = resp.text.strip().splitlines()
        assert rows[0] == "step,status,count"
        assert len(rows) >= 2

    def test_approvals_latency_csv(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t")
        state_service.record_approval_request(run_id, "p", "t", {})
        state_service.update_approval(run_id, "approved")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/approvals/latency?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
