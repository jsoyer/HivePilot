"""Tests for GET/POST /v1/autopilot -- the Autopilot state + control API.

Mirrors the analytics/memory endpoint patterns already established in
test_api_service.py (auth, tenant isolation, honest-empty) applied to
hivepilot.services.autopilot_queue / autopilot_policy instead of the
run-history tables.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivepilot.services import autopilot_queue, policy_service
from hivepilot.services.token_service import add_token


@pytest.fixture(autouse=True)
def _reset_policy_cache():
    """See tests/test_autopilot_policy.py's identical fixture -- policy_service
    caches load_policies() results until reload_policies() is called; without
    this a monkeypatched policy from one test would leak into the next."""
    policy_service.reload_policies()
    yield
    policy_service.reload_policies()


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


def _patch_policies(monkeypatch: pytest.MonkeyPatch, data: dict) -> None:
    monkeypatch.setattr(policy_service, "load_policies", lambda *a, **kw: {"policies": data})


class TestAutopilotAuth:
    def test_get_requires_auth(self, api_client):
        resp = api_client.get("/v1/autopilot")
        assert resp.status_code == 401

    def test_get_allows_read_role(self, api_client):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        assert resp.status_code == 200

    def test_pause_requires_auth(self, api_client):
        resp = api_client.post("/v1/autopilot/pause")
        assert resp.status_code == 401

    def test_pause_rejects_read_only_token(self, api_client):
        """pause/resume are CONTROL actions -- gated above 'read'."""
        raw, _ = add_token("read")
        resp = api_client.post("/v1/autopilot/pause", headers=_auth(raw))
        assert resp.status_code == 403

    def test_pause_allows_run_role(self, api_client):
        raw, _ = add_token("run")
        resp = api_client.post("/v1/autopilot/pause", headers=_auth(raw))
        assert resp.status_code == 200

    def test_resume_rejects_read_only_token(self, api_client):
        raw, _ = add_token("read")
        resp = api_client.post("/v1/autopilot/resume", headers=_auth(raw))
        assert resp.status_code == 403

    def test_resume_allows_run_role(self, api_client):
        raw, _ = add_token("run")
        resp = api_client.post("/v1/autopilot/resume", headers=_auth(raw))
        assert resp.status_code == 200


class TestAutopilotHonestEmpty:
    def test_fresh_state_is_empty_not_500(self, api_client):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["paused"] is False
        assert data["queue"] == []
        assert data["queue_depth"] == 0
        assert data["recent_dispatches"] == []

    def test_budget_daily_usd_is_null_when_unconfigured(self, api_client):
        """The repo's real policies.yaml default block never sets
        budget_daily_usd -- this must surface as a real null, never a
        fabricated number."""
        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        data = resp.json()
        assert data["budget_daily_usd"] is None
        assert data["budget_remaining"] is None
        assert data["auto_dispatch_allowlist"] == []


class TestAutopilotState:
    def test_queue_reflects_pending_rows_only(self, api_client):
        autopilot_queue.enqueue("acme-api", "groomer", "stale docs", tenant="default")
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "done one", tenant="default")
        autopilot_queue.mark(item_id, "done", cost_usd=1.5)

        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        data = resp.json()
        assert data["queue_depth"] == 1
        assert len(data["queue"]) == 1
        assert data["queue"][0]["reason"] == "stale docs"
        assert data["queue"][0]["state"] == "proposed"

    def test_recent_dispatches_reflect_done_and_blocked_rows(self, api_client):
        done_id = autopilot_queue.enqueue("acme-api", "groomer", "x", tenant="default")
        autopilot_queue.mark(done_id, "done", cost_usd=0.5)
        blocked_id = autopilot_queue.enqueue("acme-api", "reviewer", "y", tenant="default")
        autopilot_queue.mark(blocked_id, "blocked")

        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        data = resp.json()
        outcomes = {d["outcome"] for d in data["recent_dispatches"]}
        pipelines = {d["pipeline"] for d in data["recent_dispatches"]}
        assert outcomes == {"done", "blocked"}
        assert pipelines == {"groomer", "reviewer"}

    def test_budget_fields_use_configured_default_policy(self, api_client, monkeypatch):
        _patch_policies(
            monkeypatch,
            {"default": {"auto_dispatch": ["groomer"], "budget_daily_usd": 10.0}},
        )
        monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *a, **kw: 4.0)

        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        data = resp.json()
        assert data["budget_daily_usd"] == 10.0
        assert data["budget_spent_today"] == 4.0
        assert data["budget_remaining"] == 6.0
        assert data["auto_dispatch_allowlist"] == ["groomer"]

    def test_budget_remaining_never_negative(self, api_client, monkeypatch):
        _patch_policies(monkeypatch, {"default": {"budget_daily_usd": 5.0}})
        monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *a, **kw: 9.0)

        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        assert resp.json()["budget_remaining"] == 0.0

    def test_spent_today_lookup_failure_never_500s(self, api_client, monkeypatch):
        def _raise(*a, **kw):
            raise RuntimeError("analytics unavailable")

        monkeypatch.setattr(autopilot_queue, "spent_today_usd", _raise)

        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["budget_spent_today"] is None

    def test_spent_today_lookup_failure_reports_unknown_not_zero(self, api_client, monkeypatch):
        """F1: a spend-lookup failure must surface as `None` ("unknown"), not
        a fabricated `0.0` -- reporting 0.0 during an analytics outage would
        falsely reassure an operator that the full budget remains, hiding
        real accumulated spend. `budget_remaining` must ALSO go null (not
        the full ceiling) even when a positive budget IS configured."""

        def _raise(*a, **kw):
            raise RuntimeError("analytics unavailable")

        _patch_policies(monkeypatch, {"default": {"budget_daily_usd": 10.0}})
        monkeypatch.setattr(autopilot_queue, "spent_today_usd", _raise)

        raw, _ = add_token("read")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["budget_daily_usd"] == 10.0
        assert data["budget_spent_today"] is None
        assert data["budget_remaining"] is None


class TestAutopilotTenantScoping:
    def test_non_admin_sees_only_own_tenant_queue(self, api_client):
        autopilot_queue.enqueue("acme-api", "groomer", "acme item", tenant="acme")
        autopilot_queue.enqueue("acme-api", "groomer", "other item", tenant="other")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        data = resp.json()
        assert data["tenant"] == "acme"
        assert len(data["queue"]) == 1
        assert data["queue"][0]["reason"] == "acme item"

    def test_non_admin_cannot_pass_a_different_tenant(self, api_client):
        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/autopilot?tenant=other", headers=_auth(raw))
        assert resp.status_code == 403

    def test_admin_defaults_to_default_tenant(self, api_client):
        """The schedule-driven drain only ever dispatches for tenant='default'
        -- an admin with no explicit ?tenant= sees that honest default view,
        not a fabricated all-tenants aggregate."""
        autopilot_queue.enqueue("acme-api", "groomer", "default item", tenant="default")
        autopilot_queue.enqueue("acme-api", "groomer", "other item", tenant="other")

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/autopilot", headers=_auth(raw))
        data = resp.json()
        assert data["tenant"] == "default"
        assert len(data["queue"]) == 1
        assert data["queue"][0]["reason"] == "default item"

    def test_admin_can_view_a_specific_tenant(self, api_client):
        autopilot_queue.enqueue("acme-api", "groomer", "other item", tenant="other")

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/autopilot?tenant=other", headers=_auth(raw))
        data = resp.json()
        assert data["tenant"] == "other"
        assert len(data["queue"]) == 1

    def test_pause_scoped_to_own_tenant_for_non_admin(self, api_client):
        raw, _ = add_token("run", tenant="acme")
        resp = api_client.post("/v1/autopilot/pause", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["tenant"] == "acme"
        assert autopilot_queue.is_paused(tenant="acme") is True
        assert autopilot_queue.is_paused(tenant="default") is False

    def test_pause_rejects_cross_tenant_for_non_admin(self, api_client):
        raw, _ = add_token("run", tenant="acme")
        resp = api_client.post("/v1/autopilot/pause?tenant=other", headers=_auth(raw))
        assert resp.status_code == 403


class TestAutopilotControl:
    def test_pause_then_get_reflects_paused(self, api_client):
        raw_run, _ = add_token("run")
        raw_read, _ = add_token("read")

        pause_resp = api_client.post("/v1/autopilot/pause", headers=_auth(raw_run))
        assert pause_resp.status_code == 200
        assert pause_resp.json()["paused"] is True

        get_resp = api_client.get("/v1/autopilot", headers=_auth(raw_read))
        assert get_resp.json()["paused"] is True

    def test_pause_is_idempotent(self, api_client):
        raw, _ = add_token("run")
        first = api_client.post("/v1/autopilot/pause", headers=_auth(raw))
        second = api_client.post("/v1/autopilot/pause", headers=_auth(raw))
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()

    def test_resume_then_get_reflects_resumed(self, api_client):
        raw_run, _ = add_token("run")
        raw_read, _ = add_token("read")

        api_client.post("/v1/autopilot/pause", headers=_auth(raw_run))
        resume_resp = api_client.post("/v1/autopilot/resume", headers=_auth(raw_run))
        assert resume_resp.status_code == 200
        assert resume_resp.json()["paused"] is False

        get_resp = api_client.get("/v1/autopilot", headers=_auth(raw_read))
        assert get_resp.json()["paused"] is False

    def test_resume_is_idempotent_on_already_running(self, api_client):
        raw, _ = add_token("run")
        first = api_client.post("/v1/autopilot/resume", headers=_auth(raw))
        second = api_client.post("/v1/autopilot/resume", headers=_auth(raw))
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
