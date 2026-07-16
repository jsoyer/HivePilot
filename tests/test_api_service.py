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

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.complete_run(run_acme, "success")
        state_service.complete_run(run_other, "success")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/durations", headers=_auth(raw))
        assert resp.status_code == 200
        # Proves actual tenant scoping (not just reachability): only the
        # 'acme' finished run should be counted, not 'other'.
        assert resp.json()["overall"]["count"] == 1

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

    def test_summary_csv_escapes_formula_injection_in_project_name(
        self, api_client, tmp_tokens_file
    ):
        """CSV/formula-injection defense-in-depth: a project name starting
        with '=' must never reach the CSV cell unescaped — Excel/Sheets/
        LibreOffice would otherwise execute it as a formula on open."""
        from hivepilot.services import state_service

        state_service.record_run_start("=2+2", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "'=2+2" in resp.text
        # The raw, unescaped formula must not appear anywhere in the output.
        assert ",=2+2," not in resp.text

    def test_steps_failures_csv_escapes_formula_injection_in_step_name(
        self, api_client, tmp_tokens_file
    ):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "+cmd|calc", "failed")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/steps/failures?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "'+cmd|calc" in resp.text

    def test_csv_guard_only_applies_to_leading_formula_chars(self, api_client, tmp_tokens_file):
        """A normal project name must round-trip unescaped — the guard must
        not over-fire on ordinary strings."""
        from hivepilot.services import state_service

        state_service.record_run_start("normal-project", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "'normal-project" not in resp.text
        assert "normal-project" in resp.text


# ---------------------------------------------------------------------------
# Phase 24b.1 — GET /v1/analytics/providers
# ---------------------------------------------------------------------------


class TestAnalyticsProvidersAuth:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/analytics/providers")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/analytics/providers", headers=_auth(raw))
        assert resp.status_code == 403

    def test_allows_read_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/providers", headers=_auth(raw))
        assert resp.status_code == 200


class TestAnalyticsProvidersTenantIsolation:
    def test_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(run_acme, "s1", "success", provider="claude", model="m1")
        state_service.record_step(run_other, "s1", "success", provider="claude", model="m1")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/providers", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        total = sum(row["total"] for row in data["by_provider"])
        assert total == 1

    def test_admin_sees_all_tenants(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(run_acme, "s1", "success", provider="claude", model="m1")
        state_service.record_step(run_other, "s1", "success", provider="claude", model="m1")

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/analytics/providers", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        total = sum(row["total"] for row in data["by_provider"])
        assert total == 2


class TestAnalyticsProvidersShape:
    def test_json_shape(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "s1", "success", provider="claude", model="claude-x")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/providers", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert "by_provider" in data
        assert "by_model" in data
        row = data["by_provider"][0]
        assert row["provider"] == "claude"
        assert row["total"] == 1
        assert "outcomes" in row
        assert "outcome_rates" in row

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/analytics/providers", headers=_auth(raw))
        assert resp.status_code == 200

    def test_days_project_task_params_accepted(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/providers?days=7&project=p&task=t", headers=_auth(raw))
        assert resp.status_code == 200


class TestAnalyticsProvidersCsvExport:
    def test_csv_export(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "s1", "success", provider="claude", model="claude-x")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/providers?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        rows = resp.text.strip().splitlines()
        assert len(rows) >= 2  # header + at least one data row


# ---------------------------------------------------------------------------
# Phase 24b.2b — GET /v1/analytics/cost
# ---------------------------------------------------------------------------


class TestAnalyticsCostAuth:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/analytics/cost")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 403

    def test_allows_read_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 200

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 200


class TestAnalyticsCostTenantIsolation:
    def test_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(
            run_acme,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            cost_usd=1.5,
        )
        state_service.record_step(
            run_other,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            cost_usd=1.5,
        )

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["overall"]["total_steps"] == 1
        assert resp.json()["overall"]["cost_usd"] == 1.5

    def test_admin_sees_all_tenants(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(run_acme, "s1", "success", provider="claude", cost_usd=1.0)
        state_service.record_step(run_other, "s1", "success", provider="claude", cost_usd=1.0)

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["overall"]["total_steps"] == 2


class TestAnalyticsCostShape:
    def test_json_shape_includes_coverage_number(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(
            run_id, "s1", "success", provider="claude", model="unpriced-model", input_tokens=10
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert "overall" in data
        assert "by_provider" in data
        assert "by_model" in data
        assert "unpriced_steps" in data["overall"]
        assert data["overall"]["unpriced_steps"] == 1

    def test_days_project_task_params_accepted(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost?days=7&project=p&task=t", headers=_auth(raw))
        assert resp.status_code == 200


class TestAnalyticsCostCsvExport:
    def test_csv_export(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(
            run_id, "s1", "success", provider="claude", model="claude-sonnet-4-6", cost_usd=2.0
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        rows = resp.text.strip().splitlines()
        assert rows[0] == "scope,key,total_steps,input_tokens,output_tokens,cost_usd,unpriced_steps"
        assert len(rows) >= 2

    def test_cost_csv_escapes_formula_injection_in_provider_name(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "s1", "success", provider="=2+2", cost_usd=1.0)
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "'=2+2" in resp.text
        assert ",=2+2," not in resp.text

    def test_csv_escapes_formula_injection_in_provider_name(self, api_client, tmp_tokens_file):
        """CSV/formula-injection defense-in-depth: a provider value starting
        with a formula-trigger character must never reach the CSV cell
        unescaped."""
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "s1", "success", provider="=2+2", model="m")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/providers?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "'=2+2" in resp.text
        assert ",=2+2," not in resp.text
