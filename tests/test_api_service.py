"""Tests for api_service: /healthz, /readyz, /metrics endpoints.

More comprehensive observability tests live in test_observability.py.
This file exists so the TDD hook allows editing api_service.py.

The analytics endpoint tests (Phase 24a) live in this file too — they mirror
the auth/tenant-isolation patterns established in test_multi_tenant.py.
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest
import yaml
from conftest import BUNDLED_PLUGINS
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token

_HAS_FPDF = importlib.util.find_spec("fpdf") is not None


def test_startup_logs_resolved_paths(caplog):
    """Bug-debt fix: the API server must log its resolved startup paths
    ONCE, at INFO, when the process actually starts serving (the FastAPI
    `startup` lifespan event — only fires inside a `with TestClient(...)`
    context, unlike the other tests in this module)."""
    import logging as stdlib_logging

    from hivepilot.services.api_service import app
    from hivepilot.utils import startup_paths as startup_paths_mod

    # `log_resolved_startup_paths` is idempotent per `Settings` INSTANCE
    # (see its own module docstring) -- `api_service` calls it with the
    # process-wide singleton, which an EARLIER test elsewhere in the same
    # pytest process may have already logged for. Clear the module-level
    # cache so this test's own assertion is order-independent.
    startup_paths_mod._logged_for.clear()

    with caplog.at_level(stdlib_logging.INFO):
        with TestClient(app) as client:
            client.get("/healthz")

    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert "startup.resolved_paths" in rendered


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
        assert rows[0] == (
            "scope,key,total_steps,input_tokens,output_tokens,cost_usd,"
            "unpriced_steps,unpriceable_steps"
        )
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


# ---------------------------------------------------------------------------
# Pollen data endpoints sprint — /v1/analytics/cost by_project/by_role
# ---------------------------------------------------------------------------


class TestAnalyticsCostByProjectAndRole:
    def test_json_shape_includes_by_project_and_by_role(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("proj-x", "t", status="running")
        state_service.record_step(run_id, "s1", "success", provider="claude", cost_usd=1.0)
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["by_project"] == [
            {
                "project": "proj-x",
                "total_steps": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 1.0,
                "unpriced_steps": 0,
                "unpriceable_steps": 0,
                "unpriced_reasons": {},
            }
        ]
        # No role was threaded into this seeded step -> role=NULL -> the
        # honest "unknown" bucket (Mirador Agent Panels backend sprint:
        # steps.role now exists, so by_role is a REAL breakdown).
        assert data["by_role"] == [
            {
                "role": "unknown",
                "total_steps": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 1.0,
                "unpriced_steps": 0,
                "unpriceable_steps": 0,
                "unpriced_reasons": {},
            }
        ]
        assert isinstance(data["by_role_note"], str) and data["by_role_note"]
        assert data["unpriced_models"] == []

    def test_by_project_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(run_acme, "s1", "success", provider="claude", cost_usd=1.0)
        state_service.record_step(run_other, "s1", "success", provider="claude", cost_usd=1.0)

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/cost", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["by_project"][0]["total_steps"] == 1


# ---------------------------------------------------------------------------
# Pollen data endpoints sprint — GET /v1/models
# ---------------------------------------------------------------------------


class TestModelsEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/models")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/models", headers=_auth(raw))
        assert resp.status_code == 403

    def test_empty_is_empty_models_not_500(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/models", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["models"] == []
        assert data["overall"]["cost_per_successful_run"] is None
        assert data["latency_available"] is False

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/models", headers=_auth(raw))
        assert resp.status_code == 200

    def test_rollup_reflects_seeded_steps(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="success")
        state_service.record_step(
            run_id, "s1", "success", provider="claude", model="claude-sonnet-4-6", cost_usd=2.0
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/models", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == 1
        assert data["models"][0]["model"] == "claude-sonnet-4-6"
        assert data["models"][0]["cost_usd"] == 2.0
        assert data["overall"]["cost_per_successful_run"] == 2.0

    def test_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(run_acme, "s1", "success", provider="claude", cost_usd=1.0)
        state_service.record_step(run_other, "s1", "success", provider="claude", cost_usd=1.0)

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/models", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["overall"]["total_steps"] == 1

    def test_admin_sees_all_tenants(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.record_step(run_acme, "s1", "success", provider="claude", cost_usd=1.0)
        state_service.record_step(run_other, "s1", "success", provider="claude", cost_usd=1.0)

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/models", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["overall"]["total_steps"] == 2


# ---------------------------------------------------------------------------
# HP-78 — local discovery + verify-before-save
# ---------------------------------------------------------------------------


class TestOnboardingMachine:
    def test_requires_auth(self, api_client):
        assert api_client.get("/v1/onboarding/machine").status_code == 401

    def test_lists_local_and_cli(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import local_models
        from hivepilot.services.local_models import LocalBackend

        monkeypatch.setattr(
            local_models,
            "discover",
            lambda: [
                LocalBackend(
                    kind="ollama",
                    base_url="http://127.0.0.1:11434/v1",
                    reachable=True,
                    models=["llama3.2"],
                )
            ],
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/onboarding/machine", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        assert body["local"][0]["kind"] == "ollama"
        assert body["local"][0]["models"] == ["llama3.2"]
        assert any(row["kind"] == "claude" for row in body["cli"])


class TestModelsVerify:
    def test_requires_auth(self, api_client):
        assert api_client.post("/v1/models/verify", json={"provider": "ollama"}).status_code == 401

    def test_read_can_verify_local_without_key(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import model_verify as mv

        monkeypatch.setattr(
            mv,
            "verify",
            lambda provider, **kw: mv.VerifyResult(
                ok=True, target=provider, detail="ok", models=["llama3.2"]
            ),
        )
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/models/verify", headers=_auth(raw), json={"provider": "ollama"}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["models"] == ["llama3.2"]

    def test_read_cannot_submit_api_key(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/models/verify",
            headers=_auth(raw),
            json={"provider": "openai", "api_key": "sk-secret"},
        )
        assert resp.status_code == 403

    def test_rejects_ssrf_base_url(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.post(
            "/v1/models/verify",
            headers=_auth(raw),
            json={"provider": "openai", "base_url": "http://169.254.169.254/"},
        )
        assert resp.status_code == 400

    def test_agent_kind_uses_session_probe(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import model_verify as mv

        monkeypatch.setattr(
            mv,
            "verify_agent",
            lambda kind: mv.VerifyResult(
                ok=True, target=f"agent:{kind}", detail="session present"
            ),
        )
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/models/verify", headers=_auth(raw), json={"agent_kind": "claude"}
        )
        assert resp.status_code == 200
        assert resp.json()["target"] == "agent:claude"


# ---------------------------------------------------------------------------
# Pollen data endpoints sprint — GET /v1/efficiency
# ---------------------------------------------------------------------------


class TestEfficiencyEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/efficiency")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/efficiency", headers=_auth(raw))
        assert resp.status_code == 403

    def test_headroom_always_real_rtk_null_when_absent(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from hivepilot.services import api_service

        monkeypatch.setattr(api_service.efficiency_service.shutil, "which", lambda *a, **k: None)
        raw, _ = add_token("read")
        resp = api_client.get("/v1/efficiency", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["headroom"]["total_compressions"] == 0
        assert data["rtk"] is None

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import api_service

        monkeypatch.setattr(api_service.efficiency_service.shutil, "which", lambda *a, **k: None)
        raw, _ = add_token("read")
        resp = api_client.get("/efficiency", headers=_auth(raw))
        assert resp.status_code == 200

    def test_never_500s_when_rtk_shellout_raises(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import api_service

        monkeypatch.setattr(
            api_service.efficiency_service.shutil, "which", lambda *a, **k: "/usr/bin/rtk"
        )

        def _boom(*args, **kwargs):
            raise OSError("no such file")

        monkeypatch.setattr(api_service.efficiency_service.subprocess, "run", _boom)
        raw, _ = add_token("read")
        resp = api_client.get("/v1/efficiency", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["rtk"] is None

    def test_headroom_tenant_scoped(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import api_service, headroom_metrics

        monkeypatch.setattr(api_service.efficiency_service.shutil, "which", lambda *a, **k: None)
        headroom_metrics.record_compression(
            tenant="acme", step="s", chars_before=100, chars_after=10, ratio=0.1
        )
        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/efficiency", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["headroom"]["total_compressions"] == 1

        raw_other, _ = add_token("read", tenant="other")
        resp_other = api_client.get("/v1/efficiency", headers=_auth(raw_other))
        assert resp_other.json()["headroom"]["total_compressions"] == 0


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint — GET /v1/agents
# ---------------------------------------------------------------------------


class TestAgentsEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/agents")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/agents", headers=_auth(raw))
        assert resp.status_code == 403

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/agents", headers=_auth(raw))
        assert resp.status_code == 200

    def test_empty_db_full_roster_honestly_empty_not_500(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/agents", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        names = {a["name"] for a in data["agents"]}
        assert "developer" in names
        for agent in data["agents"]:
            assert agent["attributed"] is False
            assert agent["success_rate"] is None
        assert "unknown" in data
        assert isinstance(data["note"], str) and data["note"]

    def test_reflects_seeded_step_role(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="success")
        state_service.record_step(run_id, "s1", "success", cost_usd=1.0, role="developer")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/agents", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        dev = next(a for a in data["agents"] if a["name"] == "developer")
        assert dev["attributed"] is True
        assert dev["step_count"] == 1
        assert dev["cost_usd"] == 1.0

    def test_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="success", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="success", tenant="other")
        state_service.record_step(run_acme, "s1", "success", cost_usd=1.0, role="developer")
        state_service.record_step(run_other, "s1", "success", cost_usd=1.0, role="developer")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/agents", headers=_auth(raw))
        assert resp.status_code == 200
        dev = next(a for a in resp.json()["agents"] if a["name"] == "developer")
        assert dev["step_count"] == 1

    def test_admin_sees_all_tenants(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="success", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="success", tenant="other")
        state_service.record_step(run_acme, "s1", "success", cost_usd=1.0, role="developer")
        state_service.record_step(run_other, "s1", "success", cost_usd=1.0, role="developer")

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/agents", headers=_auth(raw))
        assert resp.status_code == 200
        dev = next(a for a in resp.json()["agents"] if a["name"] == "developer")
        assert dev["step_count"] == 2

    def test_invalid_days_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/agents?days=0", headers=_auth(raw))
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint — GET /v1/lessons
# ---------------------------------------------------------------------------


class TestLessonsEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/lessons")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/lessons", headers=_auth(raw))
        assert resp.status_code == 403

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/lessons", headers=_auth(raw))
        assert resp.status_code == 200

    def test_empty_db_not_500(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/lessons", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["lessons"] == []
        assert data["by_role"] == {}

    def test_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="success", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="success", tenant="other")
        state_service.record_lesson(
            run_id=run_acme,
            project="p",
            role="developer",
            task="t",
            text="acme lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        state_service.record_lesson(
            run_id=run_other,
            project="p",
            role="developer",
            task="t",
            text="other lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/lessons", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["lessons"]) == 1
        assert data["lessons"][0]["text"] == "acme lesson"

    def test_role_filter(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="success", tenant="acme")
        state_service.record_lesson(
            run_id=run_id,
            project="p",
            role="developer",
            task="t",
            text="dev lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        state_service.record_lesson(
            run_id=run_id,
            project="p",
            role="reviewer",
            task="t",
            text="reviewer lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/lessons?role=reviewer", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["lessons"]) == 1
        assert data["lessons"][0]["role"] == "reviewer"

    def test_invalid_limit_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/lessons?limit=0", headers=_auth(raw))
        assert resp.status_code == 422
        resp = api_client.get("/v1/lessons?limit=100000", headers=_auth(raw))
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint — GET /v1/verdicts
# ---------------------------------------------------------------------------


class TestVerdictsEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/verdicts")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/verdicts", headers=_auth(raw))
        assert resp.status_code == 403

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/verdicts", headers=_auth(raw))
        assert resp.status_code == 200

    def test_empty_db_not_500(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/verdicts", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdicts"] == []
        assert data["by_role"] == {}

    def test_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="success", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="success", tenant="other")
        state_service.record_verdict(
            run_id=run_acme,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="approve",
            confidence=0.9,
        )
        state_service.record_verdict(
            run_id=run_other,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="reject",
            confidence=0.9,
        )
        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/verdicts", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["verdicts"]) == 1
        assert data["verdicts"][0]["decision"] == "approve"

    def test_role_filter_and_aggregation(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="success", tenant="acme")
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="approve",
            confidence=0.9,
        )
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="developer",
            kind="debate",
            decision="approve",
            confidence=0.9,
        )
        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/verdicts?role=reviewer", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["verdicts"]) == 1
        assert data["by_role"]["reviewer"]["decision_counts"] == {"approve": 1}

    def test_invalid_limit_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/verdicts?limit=0", headers=_auth(raw))
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Pollen web UI surface (Sprint 1): GET /v1/plugins/health, GET /v1/memories
# ---------------------------------------------------------------------------


class TestPluginsHealthEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/plugins/health")
        assert resp.status_code == 401

    def test_allows_read_role_and_returns_seeded_health(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from types import SimpleNamespace

        from hivepilot.plugins import HealthStatus
        from hivepilot.services import api_service

        fake_plugins = SimpleNamespace(
            check_all=lambda: {
                "mem0": HealthStatus("ok", "self-host"),
                "rtk": HealthStatus("degraded", "not configured"),
            }
        )
        monkeypatch.setattr(
            api_service, "_get_orchestrator", lambda: SimpleNamespace(plugins=fake_plugins)
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/plugins/health", headers=_auth(raw))
        assert resp.status_code == 200
        data = {row["name"]: row for row in resp.json()["plugins"]}

        assert data["mem0"]["status"] == "ok"
        assert data["mem0"]["detail"] == "self-host"
        assert data["rtk"]["status"] == "degraded"
        assert data["rtk"]["detail"] == "not configured"

        # Health and activity are two independent answers, and the payload must
        # keep them apart. `mem0` writes telemetry, so it gets a real reading --
        # here `events == 0`, meaning "measured, and it has done nothing", which
        # is precisely the state its green `status="ok"` was hiding.
        assert data["mem0"]["activity_available"] is True
        assert data["mem0"]["activity"]["events"] == 0
        assert data["mem0"]["activity"]["last_used"] is None

        # `rtk` is a PATH check that records nothing. It must report no reading
        # at all rather than a fabricated zero, which would read as "installed
        # but idle" -- a claim no data here supports.
        assert data["rtk"]["activity_available"] is False
        assert data["rtk"]["activity"] is None

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file, monkeypatch):
        from types import SimpleNamespace

        from hivepilot.services import api_service

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(plugins=SimpleNamespace(check_all=lambda: {})),
        )
        raw, _ = add_token("read")
        resp = api_client.get("/plugins/health", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        # This test's subject is that the UNVERSIONED alias is registered, so
        # it asserts on the alias, not on the full payload shape. It used to
        # compare the whole body for equality, which made every additive field
        # a failure here rather than in the endpoint's own tests.
        assert body["plugins"] == []
        assert body["disabled"] == []
        # `denied` and `not_installed` are the two states that previously had
        # no surface at all: a plugin can be enabled and installed and still
        # not load (capability policy), and a plugin can be written and never
        # installed (they are not in the wheel).
        assert body["denied"] == []
        assert isinstance(body["not_installed"], list)

    def test_raising_check_surfaces_as_error_not_500(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """End-to-end through the REAL `PluginManager.check_all()` /
        `run_health_check()` (hivepilot/plugins.py) — not a mock of the
        endpoint's own logic — proving the actual never-crash contract, not
        just that the endpoint passes through whatever it's handed."""
        from types import SimpleNamespace

        from hivepilot.plugins import PluginManager
        from hivepilot.services import api_service

        def _boom():
            raise RuntimeError("disk on fire")

        pm = object.__new__(PluginManager)
        pm.health = {"broken": _boom}
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: SimpleNamespace(plugins=pm))

        raw, _ = add_token("read")
        resp = api_client.get("/v1/plugins/health", headers=_auth(raw))
        assert resp.status_code == 200
        entry = resp.json()["plugins"][0]
        assert entry["name"] == "broken"
        assert entry["status"] == "error"
        # The raw exception message must never reach a read-role caller...
        assert "disk on fire" not in entry["detail"]
        # ...only the exception type name is surfaced.
        assert "RuntimeError" in entry["detail"]

    def test_disabled_field_reflects_settings_plugins_disabled(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """`disabled` is a plain readback of `settings.plugins_disabled`,
        independent of `check_all()`'s (enabled-only) result -- proves the
        Health tab's re-enable rows (Pollen PRD follow-up) get their data
        from the right source, not from whatever `check_all()` happens to
        return."""
        from types import SimpleNamespace

        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(plugins=SimpleNamespace(check_all=lambda: {})),
        )
        monkeypatch.setattr(settings, "plugins_disabled", ["zeta", "rtk"])

        raw, _ = add_token("read")
        resp = api_client.get("/v1/plugins/health", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["disabled"] == ["rtk", "zeta"]

    def test_mem0_health_detail_never_leaks_api_key(self, monkeypatch):
        """Regression guard for the sprint's 'no secret in any detail'
        requirement: calls the REAL `plugins/mem0.py` `health()` with a
        configured api key and asserts the raw secret value never appears in
        the returned detail string (Phase 19 discipline).

        Loaded by file path — the SAME mechanism
        `hivepilot.plugins._scan_local_plugins` and `tests/test_mem0.py` use
        (never registers under `sys.modules["plugins"]`), so this test does
        NOT make the top-level `plugins` package importable for the rest of
        the suite (see `tests/test_plugins.py`
        `TestLoadPluginsByPath.test_loads_plugin_without_plugins_on_syspath`,
        which asserts exactly that invariant)."""
        import importlib.util

        from hivepilot.config import settings

        plugin_path = BUNDLED_PLUGINS / "mem0.py"
        spec = importlib.util.spec_from_file_location(
            "hivepilot_plugin_mem0_health_test", plugin_path
        )
        assert spec and spec.loader
        mem0_plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mem0_plugin)

        secret = "sk-super-secret-mem0-key-123"  # noqa: S105 - test fixture value
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", secret, raising=False)
        monkeypatch.setattr(mem0_plugin, "MemoryClient", lambda api_key: object())

        result = mem0_plugin.health()
        assert secret not in result.detail
        assert secret not in result.status


class TestAgentsLiveEndpoint:
    """Live per-role state, and a channel to talk to a role.

    `GET /v1/agents` answers what a role has DONE. These answer what it is
    doing right now, and let the operator say something to it -- the point of
    the herdr/Orca surface, unreachable without an endpoint.

    The honesty contract matters more than the plumbing: with no backend, or a
    failing probe, the answer is `unknown` WITH a reason. A dashboard showing
    every agent idle because it cannot see them is worse than one admitting it
    cannot see them.
    """

    def test_no_backend_reports_unknown_with_a_reason(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/agents/live", headers=_auth(raw))

        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is False
        assert body["detail"]
        assert all(a["state"] == "unknown" for a in body["agents"])

    def test_an_unknown_backend_name_is_named_not_guessed(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "agent_surface_backend", "tmux", raising=False)
        raw, _ = add_token("read")

        body = api_client.get("/v1/agents/live", headers=_auth(raw)).json()

        assert body["configured"] is False
        assert "tmux" in body["detail"]

    def test_a_failing_probe_is_unknown_not_idle(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(settings, "agent_surface_backend", "herdr", raising=False)

        class _R:
            returncode = 127
            stdout = ""

        monkeypatch.setattr(api_service, "_agent_surface_run", lambda *a, **k: _R())
        raw, _ = add_token("read")

        body = api_client.get("/v1/agents/live", headers=_auth(raw)).json()

        assert body["configured"] is True
        assert all(a["state"] == "unknown" for a in body["agents"])

    def test_an_unrecognised_state_string_becomes_unknown(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """A backend that changes its vocabulary must not smuggle a state we do
        not model into the dashboard."""
        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(settings, "agent_surface_backend", "herdr", raising=False)

        class _R:
            returncode = 0
            stdout = "reticulating-splines"

        monkeypatch.setattr(api_service, "_agent_surface_run", lambda *a, **k: _R())
        raw, _ = add_token("read")

        body = api_client.get("/v1/agents/live", headers=_auth(raw)).json()

        assert all(a["state"] == "unknown" for a in body["agents"])

    def test_the_whole_probe_is_bounded_not_just_each_call(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """22 roles x a 15s per-call timeout is 330 seconds of a blocked HTTP
        worker on a hung backend -- and Pollen polls this view. Bounding each
        call is not bounding the request.

        Past the budget the remaining roles report `unknown`, which is the same
        honest answer as any other probe failure."""
        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(settings, "agent_surface_backend", "herdr", raising=False)
        calls: list = []

        class _R:
            returncode = 0
            stdout = "idle"

        def _slow(argv, **k):
            calls.append(argv)
            return _R()

        monkeypatch.setattr(api_service, "_agent_surface_run", _slow)
        # start=0, first check under budget (one probe runs), then spent.
        ticks = iter([0.0, 0.0] + [999.0] * 200)
        monkeypatch.setattr(api_service, "_agent_surface_clock", lambda: next(ticks))
        raw, _ = add_token("read")

        body = api_client.get("/v1/agents/live", headers=_auth(raw)).json()

        assert len(calls) == 1, "the probe kept going after its budget was spent"
        assert body["agents"][-1]["state"] == "unknown"
        assert body["detail"], "a truncated probe must say why the rest is unknown"

    def test_each_probe_call_is_short(self):
        """A state read is local socket I/O. A 15s per-call timeout is a
        pipeline timeout, not a dashboard one."""
        from hivepilot.services import api_service

        assert api_service._AGENT_PROBE_TIMEOUT_S <= 3

    def test_sending_a_message_requires_run_not_read(self, api_client, tmp_tokens_file):
        """It makes something happen, so `read` must not be enough."""
        raw, _ = add_token("read")

        resp = api_client.post(
            "/v1/agents/reviewer/message", json={"text": "hi"}, headers=_auth(raw)
        )

        assert resp.status_code == 403

    def test_a_message_is_dispatched_never_claimed_delivered(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """Fire-and-forget here; claiming the agent received it would be a
        claim we cannot make."""
        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(settings, "agent_surface_backend", "herdr", raising=False)
        sent: list = []

        class _R:
            returncode = 0
            stdout = ""

        def _run(argv, **k):
            sent.append(list(argv))
            return _R()

        monkeypatch.setattr(api_service, "_agent_surface_run", _run)
        raw, _ = add_token("run")

        body = api_client.post(
            "/v1/agents/reviewer/message", json={"text": "run the tests"}, headers=_auth(raw)
        ).json()

        assert body["dispatched"] is True
        assert "delivered" not in body
        assert "run the tests" in sent[-1]
        # argv, never a shell string: the text is agent- or operator-authored.
        assert "-c" not in sent[-1]

    def test_an_empty_message_is_refused_without_dispatching(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(settings, "agent_surface_backend", "herdr", raising=False)
        monkeypatch.setattr(
            api_service,
            "_agent_surface_run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch")),
        )
        raw, _ = add_token("run")

        body = api_client.post(
            "/v1/agents/reviewer/message", json={"text": "   "}, headers=_auth(raw)
        ).json()

        assert body["dispatched"] is False


class TestMemoriesEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/memories?query=hello")
        assert resp.status_code == 401

    def test_read_role_forbidden(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/memories?query=hello", headers=_auth(raw))
        assert resp.status_code == 403

    def test_run_and_approve_roles_forbidden(self, api_client, tmp_tokens_file):
        for role in ("run", "approve"):
            raw, _ = add_token(role)
            resp = api_client.get("/v1/memories?query=hello", headers=_auth(raw))
            assert resp.status_code == 403, role

    def test_tenant_scope_guard_no_read_token_crosses_into_memories(
        self, api_client, tmp_tokens_file
    ):
        """The key risk this sprint calls out: a `read` token for ANY tenant
        must never reach mem0 memories that could belong to another tenant's
        projects. HivePilot has no tenant->project mapping to filter
        memories by (see the endpoint's own docstring), so the chosen
        mitigation is gating the whole endpoint behind `admin`. Assert that
        holds for two DIFFERENT tenants' `read` tokens — neither may read
        memories at all, so neither can ever cross into the other's data."""
        raw_a, _ = add_token("read", tenant="tenant-a")
        raw_b, _ = add_token("read", tenant="tenant-b")
        for raw in (raw_a, raw_b):
            resp = api_client.get("/v1/memories?query=hello", headers=_auth(raw))
            assert resp.status_code == 403

    def test_admin_role_allowed(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import api_service

        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: None)
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memories?query=hello", headers=_auth(raw))
        assert resp.status_code == 200
        # No client at all: `configured: False` is the RIGHT answer here, and
        # stays untouched. What changed is the FAILING-search case below --
        # see `test_search_failure_never_500s`.
        assert resp.json()["configured"] is False

    def test_empty_result_is_neither_an_error_nor_unconfigured(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """'Nothing matched' is a real, successful answer. Reporting it like a
        failure is what makes an audit of the corpus untrustworthy -- the audit
        cannot tell 'the corpus is clean' from 'the search never ran'."""
        from hivepilot.services import api_service

        class _EmptyClient:
            def search(self, *a, **k):
                return []

        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: _EmptyClient())
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memories?query=hello&user_id=acme", headers=_auth(raw))

        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["memories"] == []
        assert not body.get("error")

    def test_unconfigured_returns_graceful_200_not_500(self, api_client, tmp_tokens_file):
        """Default settings (mem0_enabled=False) — no mocking needed, this is
        the real dormant-by-default behavior."""
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memories?query=hello", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["memories"] == []

    def test_configured_returns_memories(self, api_client, tmp_tokens_file, monkeypatch):
        from unittest.mock import MagicMock

        from hivepilot.services import api_service

        mock_client = MagicMock()
        mock_client.search.return_value = {
            "results": [
                {
                    "id": "1",
                    "memory": "prefers dark mode",
                    "metadata": {"project": "acme-api"},
                    "score": 0.9,
                },
            ]
        }
        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: mock_client)
        raw, _ = add_token("admin")
        resp = api_client.get(
            "/v1/memories?query=dark+mode&limit=5&user_id=acme", headers=_auth(raw)
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["memories"][0]["memory"] == "prefers dark mode"
        assert data["memories"][0]["metadata"] == {"project": "acme-api"}
        # mem0 v3 requires a NON-EMPTY filter: no `filters` answers 400
        # "This field is required", and `filters={}` answers 400 "filters
        # cannot be empty" -- both probed against the live API. The caller
        # supplies the same `user_id` key plugins/mem0.py stores under.
        mock_client.search.assert_called_once_with(
            "dark mode", limit=5, filters={"user_id": "acme"}
        )

    def test_search_failure_never_500s(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import api_service

        class _BoomClient:
            def search(self, *a, **k):
                raise RuntimeError("mem0 backend unreachable")

        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: _BoomClient())
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memories?query=hello&user_id=acme", headers=_auth(raw))
        assert resp.status_code == 200
        # This asserted `configured is False`, which made a BROKEN search
        # indistinguishable from an ABSENT configuration -- and that is exactly
        # how the mem0 v3 breakage went unnoticed for a whole major version:
        # probed live on 2026-08-17, the endpoint answered "not configured"
        # while mem0 was configured and reachable, sending an operator to check
        # a setting that was already correct. `configured` answers "is mem0 set
        # up", and nothing else.
        body = resp.json()
        assert body["configured"] is True
        assert body["memories"] == []
        assert body["error"]

    def test_no_secret_in_response(self, api_client, tmp_tokens_file, monkeypatch):
        from unittest.mock import MagicMock

        from hivepilot.config import settings
        from hivepilot.services import api_service

        secret = "sk-real-mem0-secret-xyz"  # noqa: S105 - test fixture value
        monkeypatch.setattr(settings, "mem0_api_key", secret, raising=False)
        mock_client = MagicMock()
        mock_client.search.return_value = {"results": [{"memory": "hello world"}]}
        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: mock_client)
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memories?query=hello", headers=_auth(raw))
        assert resp.status_code == 200
        assert secret not in resp.text

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import api_service

        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: None)
        raw, _ = add_token("admin")
        resp = api_client.get("/memories?query=hello", headers=_auth(raw))
        assert resp.status_code == 200

    def test_unversioned_route_read_role_forbidden(self, api_client, tmp_tokens_file):
        """Mirrors `test_read_role_forbidden` above but against the
        unversioned `/memories` twin — the admin-only gating must hold on
        both dual-registered paths, not just the `/v1` one."""
        raw, _ = add_token("read")
        resp = api_client.get("/memories?query=hello", headers=_auth(raw))
        assert resp.status_code == 403


class TestMem0ClientHelper:
    """Unit tests for `api_service._get_mem0_client()` — mirrors
    `plugins/mem0.py`'s `_get_client()` construction logic but is a
    standalone copy (see the function's own docstring for why)."""

    def test_disabled_returns_none_without_importing(self):
        from hivepilot.config import settings
        from hivepilot.services import api_service

        assert settings.mem0_enabled is False  # dormant by default
        assert api_service._get_mem0_client() is None

    def test_missing_library_degrades_gracefully(self, monkeypatch):
        """`mem0ai` is genuinely not installed in this test environment (it's
        an optional extra, never a hivepilot dependency) — this exercises the
        real ImportError path, not a mock."""
        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        assert api_service._get_mem0_client() is None

    def test_client_construction_failure_degrades_gracefully(self, monkeypatch):
        import sys
        import types

        from hivepilot.config import settings
        from hivepilot.services import api_service

        fake_module = types.ModuleType("mem0")

        class _BoomMemory:
            def __init__(self, *a, **k):
                raise RuntimeError("bad config")

            @staticmethod
            def from_config(config):
                raise RuntimeError("bad config")

        fake_module.Memory = _BoomMemory  # type: ignore[attr-defined]
        fake_module.MemoryClient = None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mem0", fake_module)
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        monkeypatch.setattr(settings, "mem0_config", None, raising=False)

        assert api_service._get_mem0_client() is None

    def test_hosted_client_built_when_api_key_set(self, monkeypatch):
        import sys
        import types

        from hivepilot.config import settings
        from hivepilot.services import api_service

        fake_module = types.ModuleType("mem0")
        built = {}

        class _FakeMemoryClient:
            def __init__(self, api_key):
                built["api_key"] = api_key

        fake_module.Memory = None  # type: ignore[attr-defined]
        fake_module.MemoryClient = _FakeMemoryClient  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mem0", fake_module)
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", "sk-test-hosted", raising=False)

        client = api_service._get_mem0_client()
        assert isinstance(client, _FakeMemoryClient)
        assert built["api_key"] == "sk-test-hosted"

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


class TestMem0ClientParity:
    """Anti-divergence guard: `api_service._get_mem0_client()` is a
    deliberate standalone copy of `plugins/mem0.py`'s `_get_client()` (see
    both functions' docstrings for why it isn't a shared import — `plugins/`
    is user-editable/optional). Nothing enforces the two stay behaviorally
    aligned except a human reading both diffs, so this test exercises BOTH
    real implementations under the same settings and asserts they pick the
    same client-construction branch. Not a refactor — the duplication is
    intentional; this only catches silent drift between the two copies."""

    @staticmethod
    def _load_mem0_plugin_module() -> ModuleType:
        import importlib.util

        plugin_path = BUNDLED_PLUGINS / "mem0.py"
        spec = importlib.util.spec_from_file_location(
            "hivepilot_plugin_mem0_parity_test", plugin_path
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_disabled_or_unconfigured_both_return_none(self, monkeypatch):
        """Default settings: `mem0_enabled=False` and the real (not mocked)
        `mem0ai` library isn't installed in this test environment. Under
        that real, unmocked state both helpers must degrade to `None`."""
        from hivepilot.config import settings
        from hivepilot.services import api_service

        assert settings.mem0_enabled is False  # dormant by default

        mem0_plugin = self._load_mem0_plugin_module()

        assert api_service._get_mem0_client() is None
        assert mem0_plugin._get_client() is None

    def test_hosted_configured_both_build_same_client_type(self, monkeypatch):
        """`mem0_api_key` set -> both helpers must take the hosted branch
        and build an instance of the SAME `MemoryClient` type, constructed
        with the same `api_key` kwarg."""
        import sys
        import types

        from hivepilot.config import settings
        from hivepilot.services import api_service

        built: dict[str, str] = {}

        class _FakeMemoryClient:
            def __init__(self, api_key):
                built["api_key"] = api_key

        fake_module = types.ModuleType("mem0")
        fake_module.Memory = None  # type: ignore[attr-defined]
        fake_module.MemoryClient = _FakeMemoryClient  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mem0", fake_module)
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", "sk-parity-test", raising=False)

        mem0_plugin = self._load_mem0_plugin_module()
        monkeypatch.setattr(mem0_plugin, "Memory", None, raising=False)
        monkeypatch.setattr(mem0_plugin, "MemoryClient", _FakeMemoryClient, raising=False)

        api_client = api_service._get_mem0_client()
        plugin_client = mem0_plugin._get_client()

        assert type(api_client) is type(plugin_client) is _FakeMemoryClient
        assert isinstance(api_client, _FakeMemoryClient)
        assert isinstance(plugin_client, _FakeMemoryClient)

    def test_self_host_no_api_key_both_build_same_memory_type(self, monkeypatch):
        """No `mem0_api_key` -> both helpers must take the self-host branch
        and build an instance of the SAME `Memory` type."""
        import sys
        import types

        from hivepilot.config import settings
        from hivepilot.services import api_service

        class _FakeMemory:
            def __init__(self):
                pass

            @staticmethod
            def from_config(config):
                raise AssertionError("no config set — from_config must not be called")

        fake_module = types.ModuleType("mem0")
        fake_module.Memory = _FakeMemory  # type: ignore[attr-defined]
        fake_module.MemoryClient = None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mem0", fake_module)
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        monkeypatch.setattr(settings, "mem0_api_key", None, raising=False)
        monkeypatch.setattr(settings, "mem0_config", None, raising=False)

        mem0_plugin = self._load_mem0_plugin_module()
        monkeypatch.setattr(mem0_plugin, "Memory", _FakeMemory, raising=False)
        monkeypatch.setattr(mem0_plugin, "MemoryClient", None, raising=False)

        api_client = api_service._get_mem0_client()
        plugin_client = mem0_plugin._get_client()

        assert type(api_client) is type(plugin_client) is _FakeMemory


# ---------------------------------------------------------------------------
# PDF export (Phase 24 follow-up) — ?format=pdf on the analytics endpoints.
# fpdf2 is an OPTIONAL extra (pyproject.toml `pdf` extra); when it's not
# installed, ?format=pdf must fail gracefully (never a 500/traceback).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FPDF, reason="fpdf2 optional extra not installed")
class TestAnalyticsPdfExport:
    """fpdf2 is an optional extra — skip this class (not the whole module,
    so CSV/JSON regression tests below still run) when it's absent."""

    def test_summary_pdf(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")
        assert len(resp.content) > 0
        assert "attachment" in resp.headers["content-disposition"]
        assert ".pdf" in resp.headers["content-disposition"]

    def test_trends_pdf(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/trends?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_durations_pdf(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/durations?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_steps_failures_pdf(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "deploy", "failed")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/steps/failures?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_approvals_latency_pdf(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t")
        state_service.record_approval_request(run_id, "p", "t", {})
        state_service.update_approval(run_id, "approved")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/approvals/latency?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_providers_pdf(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(run_id, "s1", "success", provider="claude", model="claude-x")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/providers?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_cost_pdf(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(
            run_id, "s1", "success", provider="claude", model="claude-sonnet-4-6", cost_usd=2.0
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_pdf_requires_auth(self, api_client):
        resp = api_client.get("/v1/analytics/summary?format=pdf")
        assert resp.status_code == 401

    def test_pdf_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        """The PDF path renders the same tenant-scoped rows as JSON/CSV —
        prove real scoping, not just reachability, by checking the acme-only
        row count feeds through (mirrors TestAnalyticsTenantIsolation)."""
        from hivepilot.services import state_service

        run_acme = state_service.record_run_start("p", "t", status="running", tenant="acme")
        run_other = state_service.record_run_start("p", "t", status="running", tenant="other")
        state_service.complete_run(run_acme, "success")
        state_service.complete_run(run_other, "success")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/durations?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        # Cross-check: the JSON path (same tenant token) sees exactly 1 run.
        json_resp = api_client.get("/v1/analytics/durations", headers=_auth(raw))
        assert json_resp.json()["overall"]["count"] == 1

    def test_pdf_content_excludes_other_tenant_data(self, api_client, tmp_tokens_file):
        """Decode the actual PDF bytes (not just a cross-check against a
        separate JSON request) and prove the rendered table contains only
        the caller's tenant data — a future regression that leaked another
        tenant's rows into the PDF-specific code path would be caught here
        even though it wouldn't show up in the JSON/CSV tests."""
        import io

        from pypdf import PdfReader

        from hivepilot.services import state_service

        state_service.record_run_start("acme-project-marker", "t", status="success", tenant="acme")
        state_service.record_run_start(
            "other-project-marker", "t", status="success", tenant="other"
        )

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"

        reader = PdfReader(io.BytesIO(resp.content))
        text = "".join(page.extract_text() for page in reader.pages)
        assert "acme-project-marker" in text
        assert "other-project-marker" not in text

    def test_summary_pdf_unicode_row_does_not_crash(self, api_client, tmp_tokens_file, monkeypatch):
        """fpdf2's core fonts (Helvetica) are latin-1 only. Project/task
        names (and provider/model names sourced from LLM APIs) aren't
        guaranteed latin-1 — a non-latin-1 cell must never raise
        FPDFUnicodeEncodingException/UnicodeEncodeError inside table().

        Deterministically pinned to the NO-Unicode-font branch (never
        depends on what fonts happen to be installed on the host running
        this test) — this exercises `_pdf_safe`'s latin-1 replace path. See
        `TestAnalyticsPdfExportUnicodeFont` for the Unicode-font branch with
        the same out-of-coverage glyph, pinned to a real TTF."""
        from hivepilot.config import settings
        from hivepilot.services import api_service, state_service

        monkeypatch.setattr(settings, "pdf_font_path", None)
        monkeypatch.setattr(api_service, "_COMMON_UNICODE_FONT_PATHS", ())

        state_service.record_run_start("projet-éàü-日本語-\U0001f680", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_providers_pdf_unicode_model_name_does_not_crash(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """Same as above but through a provider/model row — model names are
        sourced from LLM APIs and not guaranteed latin-1. Deterministically
        pinned to the NO-Unicode-font branch, same reasoning as
        `test_summary_pdf_unicode_row_does_not_crash`."""
        from hivepilot.config import settings
        from hivepilot.services import api_service, state_service

        monkeypatch.setattr(settings, "pdf_font_path", None)
        monkeypatch.setattr(api_service, "_COMMON_UNICODE_FONT_PATHS", ())

        run_id = state_service.record_run_start("p", "t", status="running")
        state_service.record_step(
            run_id, "s1", "success", provider="claude", model="claude-—’emoji-\U0001f916"
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/providers?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_durations_pdf_empty_result(self, api_client, tmp_tokens_file):
        """Zero-rows path: no runs recorded yet — the PDF must still render
        (just the 'overall' row with zero counts), not error."""
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/durations?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")


def _find_any_system_ttf() -> str | None:
    """Best-effort search for ANY installed TTF, for test purposes only --
    broader than the small production candidate list in
    `api_service._COMMON_UNICODE_FONT_PATHS`, so this test still exercises
    the real-font path on a dev box that has fonts installed somewhere
    unusual (e.g. under a Flatpak runtime), while degrading to a skip on a
    genuinely fontless CI image."""
    import glob
    from pathlib import Path

    search_roots = [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        str(Path.home() / ".local/share/fonts"),
        str(Path.home() / ".local/share/flatpak/runtime"),
        "/var/lib/flatpak/runtime",
        "/Library/Fonts",
        "/System/Library/Fonts",
    ]
    for root in search_roots:
        matches = glob.glob(f"{root}/**/DejaVuSans.ttf", recursive=True)
        if matches:
            return matches[0]
    return None


@pytest.mark.skipif(not _HAS_FPDF, reason="fpdf2 optional extra not installed")
class TestAnalyticsPdfExportUnicodeFont:
    """PDF export renders non-latin text via a Unicode TTF when one is
    configured/found, and always falls back gracefully to the latin-1-only
    core font when none is available (never a crash either way)."""

    def test_no_font_configured_falls_back_to_latin1_and_still_produces_valid_pdf(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """With no `pdf_font_path` and none of the common system paths
        present, PDF export must still succeed via the pre-existing
        latin-1 fallback -- this is the byte-identical non-regression path
        and must ALWAYS be exercised, font or no font on the test box."""
        from hivepilot.config import settings
        from hivepilot.services import state_service

        monkeypatch.setattr(settings, "pdf_font_path", None)
        # Force the "no common system font found" branch too, regardless of
        # what's actually installed on the box running this test.
        monkeypatch.setattr("hivepilot.services.api_service._COMMON_UNICODE_FONT_PATHS", ())

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")

    def test_unicode_font_renders_non_latin_text_without_raising(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """With a real Unicode TTF configured, a non-latin (Cyrillic)
        project name must render without raising, producing a valid PDF."""
        font_path = _find_any_system_ttf()
        if font_path is None:
            pytest.skip("no DejaVuSans.ttf found on this box to test real Unicode rendering")

        from hivepilot.config import settings
        from hivepilot.services import state_service

        monkeypatch.setattr(settings, "pdf_font_path", font_path)

        state_service.record_run_start("проект-кириллица", "задача", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")
        assert len(resp.content) > 0

    def test_unicode_font_with_out_of_coverage_glyph_does_not_500(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """A real Unicode TTF (e.g. DejaVu Sans) still doesn't cover EVERY
        codepoint -- most emoji and much of CJK aren't in it. `Row.cell()`
        only queues text; the actual glyph lookup happens later, inside the
        `with pdf.table()` block, when `table.render()` runs -- so a naive
        try/except around `add_font`/`set_font` alone would NOT catch a
        render-time failure for an out-of-coverage codepoint. This pins a
        real font (skip only if truly none available -- deterministic
        otherwise) and asserts the response degrades gracefully (200, valid
        PDF) instead of ever 500ing, proving `_pdf_response`'s render-time
        fallback (not just its font-load fallback) actually works."""
        font_path = _find_any_system_ttf()
        if font_path is None:
            pytest.skip("no DejaVuSans.ttf found on this box to test real Unicode rendering")

        from hivepilot.config import settings
        from hivepilot.services import state_service

        monkeypatch.setattr(settings, "pdf_font_path", font_path)

        # Rocket emoji: not covered by DejaVu Sans -- exercises the
        # render-time (not just font-load) fallback path.
        state_service.record_run_start("launch-\U0001f680-project", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")
        assert len(resp.content) > 0

    def test_font_load_failure_falls_back_to_latin1_never_500s(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """A configured font path that exists but fails to load (e.g.
        corrupt/unreadable by fpdf2) must degrade to the latin-1 fallback,
        never surface a 500."""
        from hivepilot.config import settings
        from hivepilot.services import api_service, state_service

        bad_font = tmp_tokens_file.parent / "not-a-real-font.ttf"
        bad_font.write_bytes(b"not a real font file")
        monkeypatch.setattr(settings, "pdf_font_path", str(bad_font))
        monkeypatch.setattr(api_service, "_COMMON_UNICODE_FONT_PATHS", ())

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF")

    def test_resolve_unicode_font_path_prefers_configured_path(self, monkeypatch, tmp_path):
        from hivepilot.config import settings
        from hivepilot.services.api_service import _resolve_unicode_font_path

        configured = tmp_path / "custom.ttf"
        configured.write_bytes(b"stub")
        monkeypatch.setattr(settings, "pdf_font_path", str(configured))

        assert _resolve_unicode_font_path() == str(configured)

    def test_resolve_unicode_font_path_returns_none_when_nothing_found(self, monkeypatch):
        from hivepilot.config import settings
        from hivepilot.services import api_service

        monkeypatch.setattr(settings, "pdf_font_path", None)
        monkeypatch.setattr(api_service, "_COMMON_UNICODE_FONT_PATHS", ())

        assert api_service._resolve_unicode_font_path() is None


class TestAnalyticsPdfExportFpdfAbsent:
    """When fpdf2 isn't installed, ?format=pdf must return a clear 501/400
    error — never a 500/traceback."""

    @pytest.fixture()
    def no_fpdf(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "fpdf" or name.startswith("fpdf."):
                raise ImportError("No module named 'fpdf'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

    def test_summary_pdf_absent_returns_clear_error(self, api_client, tmp_tokens_file, no_fpdf):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=pdf", headers=_auth(raw))
        assert resp.status_code in (501, 400)
        assert resp.status_code != 500
        detail = resp.json()["detail"]
        assert "pdf" in detail.lower()
        assert "hivepilot[pdf]" in detail

    def test_cost_pdf_absent_returns_clear_error(self, api_client, tmp_tokens_file, no_fpdf):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost?format=pdf", headers=_auth(raw))
        assert resp.status_code in (501, 400)
        assert resp.status_code != 500
        assert "hivepilot[pdf]" in resp.json()["detail"]


class TestAnalyticsCsvAndJsonRegression:
    """?format=csv and default JSON must be unaffected by the PDF addition."""

    def test_summary_csv_unchanged(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_summary_json_default_unchanged(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        state_service.record_run_start("p", "t", status="success")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/summary", headers=_auth(raw))
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        assert "total" in resp.json()

    def test_cost_csv_unchanged(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/analytics/cost?format=csv", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]


class TestAdminReloadEndpoint:
    """POST /v1/admin/reload (Phase 14c, #249) -- admin-gated, calls
    roles.refresh_roles() + Orchestrator.refresh() and returns both bools."""

    def test_requires_auth(self, api_client):
        resp = api_client.post("/v1/admin/reload")
        assert resp.status_code == 401

    def test_read_role_forbidden(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.post("/v1/admin/reload", headers=_auth(raw))
        assert resp.status_code == 403

    def test_run_role_forbidden(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post("/v1/admin/reload", headers=_auth(raw))
        assert resp.status_code == 403

    def test_admin_role_calls_refresh_roles_and_orchestrator_refresh(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from types import SimpleNamespace

        from hivepilot.services import api_service

        refresh_roles_calls = []
        orch_refresh_calls = []

        monkeypatch.setattr(
            api_service.roles,
            "refresh_roles",
            lambda: refresh_roles_calls.append(1) or True,
        )
        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(refresh=lambda: orch_refresh_calls.append(1) or True),
        )

        raw, _ = add_token("admin")
        resp = api_client.post("/v1/admin/reload", headers=_auth(raw))

        assert resp.status_code == 200
        assert resp.json() == {"roles_reloaded": True, "config_reloaded": True}
        assert refresh_roles_calls == [1]
        assert orch_refresh_calls == [1]

    def test_partial_failure_reported_not_hidden(self, api_client, tmp_tokens_file, monkeypatch):
        """A broken roles.yaml (refresh_roles() -> False) must surface as
        `roles_reloaded: false` in the response, not be swallowed into a
        blanket success."""
        from types import SimpleNamespace

        from hivepilot.services import api_service

        monkeypatch.setattr(api_service.roles, "refresh_roles", lambda: False)
        monkeypatch.setattr(
            api_service, "_get_orchestrator", lambda: SimpleNamespace(refresh=lambda: True)
        )

        raw, _ = add_token("admin")
        resp = api_client.post("/v1/admin/reload", headers=_auth(raw))

        assert resp.status_code == 200
        assert resp.json() == {"roles_reloaded": False, "config_reloaded": True}

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file, monkeypatch):
        from types import SimpleNamespace

        from hivepilot.services import api_service

        monkeypatch.setattr(api_service.roles, "refresh_roles", lambda: True)
        monkeypatch.setattr(
            api_service, "_get_orchestrator", lambda: SimpleNamespace(refresh=lambda: True)
        )

        raw, _ = add_token("admin")
        resp = api_client.post("/admin/reload", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"roles_reloaded": True, "config_reloaded": True}


# ---------------------------------------------------------------------------
# `POST /v1/approvals/{run_id}` (Pollen's "Approve" button) -- regression
# for the live bug: this endpoint called `Orchestrator.run_approved`
# unconditionally, and `run_approved` does `self.tasks.tasks[task_name]` --
# for a pipeline checkpoint `task_name` is actually the PIPELINE name
# (e.g. "noxys", not a task), so it raised a bare `KeyError` -> 500. The
# endpoint now goes through `Orchestrator.approve_run`, the single shared
# helper also used by `telegram_bot._dispatch_approval` (see
# tests/test_pipeline_checkpoint.py::TestApproveRunRouting for the routing
# logic itself, unit-tested directly on the orchestrator).
# ---------------------------------------------------------------------------


class _FakeApprovalOrchestrator:
    """Real `approve_run` bound to fake `resume_pipeline`/`run_approved` --
    exercises the ACTUAL routing method through the API, not a re-implemented
    stand-in, while stubbing out the heavy pipeline/task execution."""

    def __init__(self) -> None:
        self.resume_pipeline_calls: list[dict] = []
        self.run_approved_calls: list[dict] = []

    def resume_pipeline(self, **kwargs):
        from hivepilot.orchestrator import RunResult

        self.resume_pipeline_calls.append(kwargs)
        return RunResult("noxys", "noxys", kwargs.get("approve", True))

    def run_approved(self, **kwargs):
        from hivepilot.orchestrator import RunResult

        self.run_approved_calls.append(kwargs)
        return RunResult("proj", "task", kwargs.get("approve", True))


# Bind the REAL `Orchestrator.approve_run` implementation onto the fake so
# the test exercises production routing logic, not a re-implementation of it.
from hivepilot.orchestrator import Orchestrator as _Orchestrator  # noqa: E402

_FakeApprovalOrchestrator.approve_run = _Orchestrator.approve_run  # type: ignore[attr-defined]


class TestApprovalEndpointRouting:
    def test_pipeline_checkpoint_approval_routes_to_resume_pipeline_not_run_approved(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """The live-bug regression, exercised through the real API endpoint:
        approving a pipeline-checkpoint run must return 200 and hit
        `resume_pipeline`, never `run_approved` (which historically
        KeyError'd on the pipeline name)."""
        from hivepilot.services import api_service, state_service

        run_id = state_service.record_run_start("proj", "noxys", status="pending")
        state_service.record_approval_request(
            run_id=run_id,
            project="proj",
            task="noxys",  # the pipeline name -- NOT a task -- is what KeyErrors
            metadata={"kind": "pipeline_checkpoint", "pipeline": "noxys"},
        )

        fake_orch = _FakeApprovalOrchestrator()
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: fake_orch)

        raw, _ = add_token("admin")
        resp = api_client.post(
            f"/v1/approvals/{run_id}",
            json={"approver": "mirador", "approve": True},
            headers=_auth(raw),
        )

        assert resp.status_code == 200
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.run_approved_calls == []

    def test_per_task_approval_still_routes_to_run_approved(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """A plain per-task approval (no `pipeline_checkpoint` metadata) via
        the API must still route to `run_approved` -- unchanged behavior."""
        from hivepilot.services import api_service, state_service

        run_id = state_service.record_run_start("proj", "build", status="pending")
        state_service.record_approval_request(
            run_id=run_id, project="proj", task="build", metadata={}
        )

        fake_orch = _FakeApprovalOrchestrator()
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: fake_orch)

        raw, _ = add_token("admin")
        resp = api_client.post(
            f"/v1/approvals/{run_id}",
            json={"approver": "mirador", "approve": True},
            headers=_auth(raw),
        )

        assert resp.status_code == 200
        assert len(fake_orch.run_approved_calls) == 1
        assert fake_orch.resume_pipeline_calls == []

    def test_deny_pipeline_checkpoint_routes_to_resume_pipeline(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """Denying a pipeline checkpoint via the API must also route to
        `resume_pipeline` (approve=False), not `run_approved`."""
        from hivepilot.services import api_service, state_service

        run_id = state_service.record_run_start("proj", "noxys", status="pending")
        state_service.record_approval_request(
            run_id=run_id,
            project="proj",
            task="noxys",
            metadata={"kind": "pipeline_checkpoint", "pipeline": "noxys"},
        )

        fake_orch = _FakeApprovalOrchestrator()
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: fake_orch)

        raw, _ = add_token("admin")
        resp = api_client.post(
            f"/v1/approvals/{run_id}",
            json={"approver": "mirador", "approve": False},
            headers=_auth(raw),
        )

        assert resp.status_code == 200
        assert len(fake_orch.resume_pipeline_calls) == 1
        assert fake_orch.resume_pipeline_calls[0]["approve"] is False
        assert fake_orch.run_approved_calls == []

    def test_unknown_run_returns_clean_400_not_500(self, api_client, tmp_tokens_file, monkeypatch):
        """An unknown/not-pending run must return a clean 4xx, never an
        unhandled exception / 500."""
        from hivepilot.services import api_service

        fake_orch = _FakeApprovalOrchestrator()
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: fake_orch)

        raw, _ = add_token("admin")
        resp = api_client.post(
            "/v1/approvals/999999",
            json={"approver": "mirador", "approve": True},
            headers=_auth(raw),
        )

        assert resp.status_code == 400
        assert "not pending approval" in resp.json()["detail"]

    def test_already_resolved_run_returns_clean_400_not_500(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """A run whose approval was already resolved must return a clean
        4xx, never a 500."""
        from hivepilot.services import api_service, state_service

        run_id = state_service.record_run_start("proj", "build", status="approved")
        state_service.record_approval_request(
            run_id=run_id, project="proj", task="build", metadata={}
        )
        state_service.update_approval(run_id, "approved", "someone")

        fake_orch = _FakeApprovalOrchestrator()
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: fake_orch)

        raw, _ = add_token("admin")
        resp = api_client.post(
            f"/v1/approvals/{run_id}",
            json={"approver": "mirador", "approve": True},
            headers=_auth(raw),
        )

        assert resp.status_code == 400

    def test_no_direct_run_approved_call_in_api_service_source(self) -> None:
        """Static guard: the routing decision must live in ONE place
        (`Orchestrator.approve_run`) -- `api_service.py` must never call
        `run_approved`/`resume_pipeline` directly again."""
        from pathlib import Path

        from hivepilot.services import api_service

        source = Path(api_service.__file__).read_text()
        assert ".run_approved(" not in source
        assert ".resume_pipeline(" not in source
        assert ".approve_run(" in source


class TestHandleApprovalExplicitFailures:
    """Explicit-failure-logs sprint, Part A.2: a known rejection reason from
    `Orchestrator.approve_run` (raised as `ValueError`/`KeyError` -- the
    routing dispatch re-raises whatever `resume_pipeline`/`run_approved`
    raise) must surface as a clean 400 with that reason in the body -- never
    an opaque 500 -- and every attempt/failure is logged with structured
    context. Mocks `approve_run` directly (not `run_approved`) since that is
    the entrypoint `handle_approval` now calls -- these tests are about
    `api_service`'s OWN exception-to-HTTP-status translation, independent of
    the routing logic itself (covered by `TestApprovalEndpointRouting`
    above)."""

    def test_not_pending_becomes_400_not_500(self, api_client, tmp_tokens_file, monkeypatch):
        from types import SimpleNamespace

        from hivepilot.services import api_service

        def _boom(**kwargs):
            raise ValueError("Run 7 is not pending approval (current status='denied').")

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(approve_run=_boom),
        )
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/approvals/7", json={"approver": "tester"}, headers=_auth(raw))

        assert resp.status_code == 400
        assert "not pending approval" in resp.json()["detail"]

    def test_unknown_task_becomes_400_not_500(self, api_client, tmp_tokens_file, monkeypatch):
        """The `self.tasks.tasks[task_name]` bare `KeyError` this sprint fixed
        at the orchestrator layer (now a `ValueError`) -- still exercised end-
        to-end here as a plain `KeyError` too, since `handle_approval` must
        catch BOTH exception types cleanly."""
        from types import SimpleNamespace

        from hivepilot.services import api_service

        def _boom(**kwargs):
            raise KeyError("stale-task-name")

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(approve_run=_boom),
        )
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/approvals/7", json={"approver": "tester"}, headers=_auth(raw))

        assert resp.status_code == 400

    def test_unexpected_error_still_logged_before_500(
        self, api_client, tmp_tokens_file, monkeypatch, caplog
    ):
        import logging as stdlib_logging
        from types import SimpleNamespace

        from hivepilot.services import api_service

        def _boom(**kwargs):
            raise RuntimeError("totally unexpected internal failure")

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(approve_run=_boom),
        )
        raw, _ = add_token("admin")
        with caplog.at_level(stdlib_logging.INFO):
            resp = api_client.post(
                "/v1/approvals/7", json={"approver": "tester"}, headers=_auth(raw)
            )

        assert resp.status_code == 500
        assert "totally unexpected internal failure" in resp.json()["detail"]
        failed_records = [
            r for r in caplog.records if "api.approval.failed_unexpected" in r.message
        ]
        assert failed_records

    def test_success_path_returns_result(self, api_client, tmp_tokens_file, monkeypatch):
        from types import SimpleNamespace

        from hivepilot.services import api_service

        class _Result:
            __dict__ = {"success": True, "detail": "ok"}

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(approve_run=lambda **kwargs: _Result()),
        )
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/approvals/7", json={"approver": "tester"}, headers=_auth(raw))

        assert resp.status_code == 200
        assert resp.json()["result"]["success"] is True


class TestPluginsHealthSurfacesTheSilentStates:
    """`check_all()` only covers REGISTERED plugins.

    Two states were therefore invisible in every UI, and both mean "you think
    this is running and it is not":

    - **denied** — enabled AND installed, rolled back before registration
      because its declared capability is outside
      `plugins_capability_policy`. Not in the healthy list, not in the
      disabled list. Observed live: `token_savior` loads under the services'
      policy and is denied under a CLI environment that lacks it — same
      plugin, same flag, opposite outcome, and the UI showed nothing either
      way.
    - **not_installed** — written in the repo, never fetched onto this host.
      Plugins are not shipped in the wheel, so a merge does not install them.
      Reporting only what IS installed answers "what is on" while hiding
      "what exists", which is how ~23 written plugins sat inert here.
    """

    def test_a_capability_denied_plugin_is_reported(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from hivepilot.services import api_service

        record = SimpleNamespace(name="token_savior", source="local-file")
        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(
                plugins=SimpleNamespace(
                    check_all=lambda: {},
                    denied=[(record, "declares ['filesystem'] not permitted")],
                )
            ),
        )
        raw, _ = add_token("read")

        body = api_client.get("/v1/plugins/health", headers=_auth(raw)).json()

        assert [d["name"] for d in body["denied"]] == ["token_savior"]
        assert "filesystem" in body["denied"][0]["error"]

    def test_the_denial_says_how_to_fix_it(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A denial an operator cannot act on is only marginally better than
        silence."""
        from types import SimpleNamespace

        from hivepilot.services import api_service

        record = SimpleNamespace(name="token_savior", source="local-file")
        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(
                plugins=SimpleNamespace(check_all=lambda: {}, denied=[(record, "nope")])
            ),
        )
        raw, _ = add_token("read")

        body = api_client.get("/v1/plugins/health", headers=_auth(raw)).json()

        assert "PLUGINS_CAPABILITY_POLICY" in body["denied"][0]["remediation"]

    def test_a_manager_without_denied_does_not_break_the_endpoint(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Health must never 500 — an older manager simply reports none."""
        from types import SimpleNamespace

        from hivepilot.services import api_service

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(plugins=SimpleNamespace(check_all=lambda: {})),
        )
        raw, _ = add_token("read")

        resp = api_client.get("/v1/plugins/health", headers=_auth(raw))

        assert resp.status_code == 200
        assert resp.json()["denied"] == []

    def test_written_but_uninstalled_plugins_are_listed(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from hivepilot.services import api_service
        from hivepilot.services import plugin_installer as pi

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(plugins=SimpleNamespace(check_all=lambda: {}, denied=[])),
        )
        monkeypatch.setattr(pi, "is_installed", lambda name: name == "rtk")
        raw, _ = add_token("read")

        body = api_client.get("/v1/plugins/health", headers=_auth(raw)).json()

        assert "rtk" not in body["not_installed"]
        assert "onepassword" in body["not_installed"]


class TestPluginCatalogEndpoint:
    """The card UI needs the plugins that EXIST, not the ones that loaded.

    `/plugins/health` only reports registered plugins, so a page built on it
    could never show the ~23 curated plugins that are written and not
    installed — which is exactly the set an operator wants to browse and
    turn on.
    """

    def test_it_lists_every_curated_plugin_not_just_the_loaded_ones(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from hivepilot.services import api_service

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(plugins=SimpleNamespace(check_all=lambda: {}, denied=[])),
        )
        raw, _ = add_token("read")

        body = api_client.get("/v1/plugins/catalog", headers=_auth(raw)).json()
        names = {p["name"] for p in body["plugins"]}

        assert "onepassword" in names
        assert "rtk" in names
        assert len(names) > 10

    def test_each_entry_carries_what_a_card_must_show(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A switch with no description is a switch nobody dares flip."""
        from types import SimpleNamespace

        from hivepilot.services import api_service

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(plugins=SimpleNamespace(check_all=lambda: {}, denied=[])),
        )
        raw, _ = add_token("read")

        body = api_client.get("/v1/plugins/catalog", headers=_auth(raw)).json()
        entry = next(p for p in body["plugins"] if p["name"] == "onepassword")

        assert entry["description"]
        assert entry["prereq_detail"]
        assert entry["prereq_kind"]
        assert "installed" in entry
        assert "enabled" in entry

    def test_it_never_leaks_a_secret_value(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Catalog metadata only — this endpoint is `read`-role, unlike the
        admin-gated install/toggle ones."""
        from types import SimpleNamespace

        from hivepilot.services import api_service

        monkeypatch.setattr(
            api_service,
            "_get_orchestrator",
            lambda: SimpleNamespace(plugins=SimpleNamespace(check_all=lambda: {}, denied=[])),
        )
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "op_service_account_token", "tok-LEAK-canary", raising=False)
        raw, _ = add_token("read")

        assert (
            "tok-LEAK-canary" not in api_client.get("/v1/plugins/catalog", headers=_auth(raw)).text
        )


class TestPluginInstallEndpoint:
    """Flipping a switch on a plugin that is not installed has to be able to
    install it, or the switch is decorative."""

    def test_it_refuses_a_name_outside_the_curated_registry(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed BEFORE any fetch: this writes executable Python onto
        the host, so an arbitrary name must never reach the fetcher."""
        from hivepilot.services import api_service, plugin_installer

        called: list[str] = []
        monkeypatch.setattr(plugin_installer, "fetch_plugin", lambda name, **k: called.append(name))
        raw, _ = add_token("admin")

        resp = api_client.post("/v1/plugins/..%2Fevil/install", headers=_auth(raw))

        assert resp.status_code in (400, 404)
        assert called == []
        assert api_service is not None

    def test_a_read_token_cannot_install(self, api_client) -> None:
        """It modifies the host and stages code that runs in-process."""
        raw, _ = add_token("read")

        assert api_client.post("/v1/plugins/rtk/install", headers=_auth(raw)).status_code == 403

    def test_installing_persists_the_enable_flag(
        self, api_client, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from hivepilot.services import plugin_installer

        persisted: list[str] = []
        monkeypatch.setattr(
            plugin_installer, "fetch_plugin", lambda name, **k: tmp_path / f"{name}.py"
        )

        def _persist(name: str, **_k: object):
            persisted.append(name)
            return tmp_path / ".env"

        monkeypatch.setattr(plugin_installer, "persist_enabled", _persist)
        raw, _ = add_token("admin")

        resp = api_client.post("/v1/plugins/rtk/install", headers=_auth(raw))

        assert resp.status_code == 200
        assert persisted == ["rtk"]

    def test_the_response_says_a_restart_is_needed(
        self, api_client, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """`PluginManager` scans once at construction, so a freshly installed
        plugin is inert until the process restarts. A UI that implied
        otherwise would have the operator hunting a plugin that is on disk,
        enabled, and doing nothing."""
        from hivepilot.services import plugin_installer

        monkeypatch.setattr(
            plugin_installer, "fetch_plugin", lambda name, **k: tmp_path / f"{name}.py"
        )
        monkeypatch.setattr(
            plugin_installer, "persist_enabled", lambda name, **k: tmp_path / ".env"
        )
        raw, _ = add_token("admin")

        body = api_client.post("/v1/plugins/rtk/install", headers=_auth(raw)).json()

        assert body["restart_required"] is True
        assert body["prereq_detail"]


class TestAgentAdminEndpoints:
    """Agent binaries admin (#29): the routes are the replacement guarantee
    for agent_install.py's TTY-only wall — admin role + an explicit consent
    field, with the actor and both versions in the audit row.

    Lives HERE and not in tests/test_agent_admin.py: that file sorts to
    alphabetical position 2, and importing api_service that early broke 51
    unrelated tests (bisected). Driven as functions — the endpoint logic is
    what's under test; route registration is asserted by introspection."""

    @staticmethod
    def _call(kind, action, consent, monkeypatch, fake=None):
        from fastapi import HTTPException

        from hivepilot.services import agent_admin as aa
        from hivepilot.services.api_service import AgentActionRequest, agent_action_endpoint
        from hivepilot.services.token_service import TokenEntry

        if fake is not None:
            monkeypatch.setattr(aa, "perform_agent_action", fake)
        caller = TokenEntry(token="h" * 64, role="admin", note="jerome")
        try:
            return agent_action_endpoint(kind, action, AgentActionRequest(consent=consent), caller)
        except HTTPException as exc:
            return exc

    def test_an_action_without_consent_is_refused_naming_it(self, monkeypatch):
        """`consent: true` is the button's signature on the decision — the
        non-interactive replacement for the TTY 'yes'. Absent or false, the
        service is never reached."""
        called: list = []
        result = self._call(
            "grok", "update", False, monkeypatch, fake=lambda *a, **k: called.append(a)
        )

        assert getattr(result, "status_code", None) == 400
        assert "consent" in result.detail
        assert called == []

    def test_the_request_model_defaults_consent_to_false(self):
        """Absent-means-no is the only safe default for a field that
        authorises running a vendor's install pipeline."""
        from hivepilot.services.api_service import AgentActionRequest

        assert AgentActionRequest().consent is False

    def test_a_consented_action_reaches_the_service_with_the_actor(self, monkeypatch):
        seen: dict = {}

        def _fake(kind, action, *, actor, token_hash=""):
            seen.update(kind=kind, action=action, actor=actor, token_hash=token_hash)
            return {"kind": kind, "action": action, "ok": True}

        result = self._call("grok", "update", True, monkeypatch, fake=_fake)

        assert result == {"kind": "grok", "action": "update", "ok": True}
        assert seen["actor"] == "jerome", "the audit trail needs a who"
        assert seen["token_hash"], "and the token hash beside it"

    def test_a_service_refusal_becomes_a_400_not_a_500(self, monkeypatch):
        """An unknown kind or a docs-only install is an operator mistake, not
        a server fault — the distinction decides what Pollen shows."""
        result = self._call("not-a-kind", "update", True, monkeypatch)

        assert getattr(result, "status_code", None) == 400
        assert "unknown agent kind" in result.detail

    def test_both_routes_are_registered_with_admin_dependencies(self):
        """Introspection, no request: the paths exist on the app and each
        carries a dependency chain (the require_role gate). A route that
        vanished — or shipped ungated — fails here without ever starting the
        app."""
        from hivepilot.services.api_service import app, v1

        # Scanned over app ∪ v1: where a route materialises depends on
        # include_router timing that this test has no business pinning — two
        # separate probes of `app.routes` alone disagreed with each other.
        # What matters is that both spellings exist somewhere real and carry
        # their role gate.
        by_path = {}
        for route in list(app.routes) + list(v1.routes):
            path = getattr(route, "path", "")
            if path in (
                "/agents/admin",
                "/v1/agents/admin",
                "/agents/{kind}/{action}",
                "/v1/agents/{kind}/{action}",
            ):
                by_path[path.removeprefix("/v1")] = route

        assert set(by_path) == {"/agents/admin", "/agents/{kind}/{action}"}
        for route in by_path.values():
            assert route.dependant.dependencies, f"{route.path} shipped without its role gate"


class TestAgentLoginEndpoint:
    """#33's API half — lives HERE for the same load-bearing-location reason
    as TestAgentAdminEndpoints above."""

    @staticmethod
    def _call(kind, consent, monkeypatch, result=None, error=None):
        from fastapi import HTTPException

        from hivepilot.services import agent_auth, state_service
        from hivepilot.services.api_service import AgentActionRequest, agent_login_endpoint
        from hivepilot.services.token_service import TokenEntry

        audits: list = []
        monkeypatch.setattr(state_service, "record_audit", lambda **kw: audits.append(kw))
        if error is not None:
            monkeypatch.setattr(
                agent_auth,
                "start_headless_login",
                lambda k: (_ for _ in ()).throw(agent_auth.AgentAuthError(error)),
            )
        else:
            monkeypatch.setattr(agent_auth, "start_headless_login", lambda k: result)
        caller = TokenEntry(token="h" * 64, role="admin", note="jerome")
        try:
            return agent_login_endpoint(kind, AgentActionRequest(consent=consent), caller), audits
        except HTTPException as exc:
            return exc, audits

    def test_consent_is_required(self, monkeypatch):
        response, audits = self._call("grok", False, monkeypatch, result={"url": "x", "log": "y"})

        assert response.status_code == 400 and "consent" in response.detail
        assert audits == []

    def test_it_returns_the_url_and_records_that_a_login_started(self, monkeypatch):
        response, audits = self._call(
            "grok",
            True,
            monkeypatch,
            result={"kind": "grok", "url": "https://accounts.x.ai/a?c=1", "log": "/x.log"},
        )

        assert response["url"] == "https://accounts.x.ai/a?c=1"
        assert audits and audits[0]["result"] == "login started"
        assert "token" not in str(audits[0]).lower() or "token_hash" in str(audits[0])

    def test_an_unverified_kind_is_a_400_naming_the_verified_ones(self, monkeypatch):
        response, _ = self._call(
            "opencode", True, monkeypatch, error="'opencode' has no verified headless login flow"
        )

        assert response.status_code == 400
        assert "no verified headless login" in response.detail
