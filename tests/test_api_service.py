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
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token

_HAS_FPDF = importlib.util.find_spec("fpdf") is not None


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


# ---------------------------------------------------------------------------
# Mirador web UI surface (Sprint 1): GET /v1/plugins/health, GET /v1/memories
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
        data = resp.json()["plugins"]
        assert {"name": "mem0", "status": "ok", "detail": "self-host"} in data
        assert {"name": "rtk", "status": "degraded", "detail": "not configured"} in data

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
        assert resp.json() == {"plugins": [], "disabled": []}

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
        Health tab's re-enable rows (Mirador PRD follow-up) get their data
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
        from pathlib import Path

        from hivepilot.config import settings

        plugin_path = Path(__file__).resolve().parent.parent / "plugins" / "mem0.py"
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
        assert resp.json()["configured"] is False

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
        resp = api_client.get("/v1/memories?query=dark+mode&limit=5", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["memories"][0]["memory"] == "prefers dark mode"
        assert data["memories"][0]["metadata"] == {"project": "acme-api"}
        mock_client.search.assert_called_once_with("dark mode", limit=5)

    def test_search_failure_never_500s(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import api_service

        class _BoomClient:
            def search(self, *a, **k):
                raise RuntimeError("mem0 backend unreachable")

        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: _BoomClient())
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memories?query=hello", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

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
        from pathlib import Path

        plugin_path = Path(__file__).resolve().parent.parent / "plugins" / "mem0.py"
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
