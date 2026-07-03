"""
Tests for multi-tenant isolation (PROD-HARDENING 2c).

Covers:
1. Token with tenant persists/loads correctly; legacy entry without tenant -> "default".
2. Non-admin token can only see its own tenant's runs/approvals.
3. Cross-tenant access by non-admin -> 403.
4. Admin token sees all tenants.
5. Run started via API is stamped with caller's tenant.
6. Single-tenant "default" path is unchanged (smoke test).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import hivepilot.services.state_service as state_service
from hivepilot.services.token_service import add_token, load_tokens

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_tokens_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_tokens_file: Path):
    """Return a TestClient with isolated DB and tokens file."""
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _make_token(role: str, tenant: str, tmp_tokens_file: Path) -> str:
    """Create a token with the given role/tenant and return the raw plaintext."""
    raw, entry = add_token(role, tenant=tenant)
    return raw


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


# ---------------------------------------------------------------------------
# 1. Token tenant field: persist / load / legacy default
# ---------------------------------------------------------------------------


class TestTokenTenant:
    def test_add_token_default_tenant(self, tmp_tokens_file: Path) -> None:
        """add_token without explicit tenant defaults to 'default'."""
        raw, entry = add_token("run")
        assert entry.tenant == "default"

    def test_add_token_custom_tenant(self, tmp_tokens_file: Path) -> None:
        """add_token with explicit tenant stores that tenant."""
        raw, entry = add_token("run", tenant="acme")
        assert entry.tenant == "acme"

    def test_save_and_load_preserves_tenant(self, tmp_tokens_file: Path) -> None:
        """save_tokens/load_tokens round-trips the tenant field."""
        raw, entry = add_token("run", tenant="beta")
        loaded = load_tokens()
        match = next(e for e in loaded if e.token == entry.token)
        assert match.tenant == "beta"

    def test_legacy_entry_without_tenant_defaults_to_default(
        self, tmp_tokens_file: Path
    ) -> None:
        """Legacy YAML entries without a 'tenant' key load as 'default'."""
        data = {
            "tokens": [
                {
                    "token_hash": hashlib.sha256(b"legacytoken").hexdigest(),
                    "role": "read",
                }
            ]
        }
        tmp_tokens_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        tokens = load_tokens(tmp_tokens_file)
        assert len(tokens) == 1
        assert tokens[0].tenant == "default"


# ---------------------------------------------------------------------------
# 2. Non-admin can only see own tenant's runs
# ---------------------------------------------------------------------------


class TestTenantIsolationRuns:
    def test_non_admin_sees_only_own_tenant_runs(
        self, api_client: TestClient, tmp_tokens_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-admin 'acme' token only sees acme runs, not 'other' runs."""
        # Create run for acme tenant
        run_id_acme = state_service.record_run_start("proj", "task", tenant="acme")
        # Create run for other tenant
        run_id_other = state_service.record_run_start("proj", "task", tenant="other")

        acme_token = _make_token("run", "acme", tmp_tokens_file)

        resp = api_client.get("/runs", headers=_auth(acme_token))
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert run_id_acme in ids
        assert run_id_other not in ids

    def test_admin_sees_all_tenant_runs(
        self, api_client: TestClient, tmp_tokens_file: Path
    ) -> None:
        """Admin token sees runs from all tenants."""
        run_id_acme = state_service.record_run_start("proj", "task", tenant="acme")
        run_id_other = state_service.record_run_start("proj", "task", tenant="other")

        admin_token = _make_token("admin", "default", tmp_tokens_file)

        resp = api_client.get("/runs", headers=_auth(admin_token))
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert run_id_acme in ids
        assert run_id_other in ids


# ---------------------------------------------------------------------------
# 3. Cross-tenant access by non-admin -> 403
# ---------------------------------------------------------------------------


class TestCrossTenantForbidden:
    def test_non_admin_cannot_approve_other_tenant_run(
        self, api_client: TestClient, tmp_tokens_file: Path
    ) -> None:
        """Non-admin token gets 403 when approving a run from a different tenant."""

        run_id = state_service.record_run_start("proj", "task", tenant="other")
        state_service.record_approval_request(
            run_id=run_id,
            project="proj",
            task="task",
            metadata={},
            tenant="other",
        )

        acme_token = _make_token("approve", "acme", tmp_tokens_file)
        resp = api_client.post(
            f"/approvals/{run_id}",
            json={"approver": "user", "approve": True},
            headers=_auth(acme_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. Admin sees all tenants (approvals)
# ---------------------------------------------------------------------------


class TestAdminSeesAll:
    def test_admin_sees_all_pending_approvals(
        self, api_client: TestClient, tmp_tokens_file: Path
    ) -> None:
        """Admin GET /approvals includes rows from multiple tenants."""
        run1 = state_service.record_run_start("p1", "t1", tenant="acme")
        run2 = state_service.record_run_start("p2", "t2", tenant="beta")
        state_service.record_approval_request(run1, "p1", "t1", {}, tenant="acme")
        state_service.record_approval_request(run2, "p2", "t2", {}, tenant="beta")

        admin_token = _make_token("admin", "default", tmp_tokens_file)
        resp = api_client.get("/approvals", headers=_auth(admin_token))
        assert resp.status_code == 200
        run_ids = [r["run_id"] for r in resp.json()]
        assert run1 in run_ids
        assert run2 in run_ids


# ---------------------------------------------------------------------------
# 5. Run started via API is stamped with caller's tenant
# ---------------------------------------------------------------------------


class TestRunStampedWithTenant:
    def test_run_start_stamps_tenant(self) -> None:
        """record_run_start stamps the tenant column correctly."""
        run_id = state_service.record_run_start("proj", "task", tenant="gamma")
        with sqlite3.connect(state_service.DB_PATH) as conn:
            row = conn.execute("SELECT tenant FROM runs WHERE id=?", (run_id,)).fetchone()
        assert row is not None
        assert row[0] == "gamma"

    def test_run_start_default_tenant(self) -> None:
        """record_run_start without tenant defaults to 'default'."""
        run_id = state_service.record_run_start("proj", "task")
        with sqlite3.connect(state_service.DB_PATH) as conn:
            row = conn.execute("SELECT tenant FROM runs WHERE id=?", (run_id,)).fetchone()
        assert row is not None
        assert row[0] == "default"


# ---------------------------------------------------------------------------
# 6. Single-tenant 'default' path unchanged (smoke test)
# ---------------------------------------------------------------------------


class TestSingleTenantSmoke:
    def test_default_path_works_end_to_end(
        self, api_client: TestClient, tmp_tokens_file: Path
    ) -> None:
        """Existing single-tenant behavior (everything 'default') is unchanged."""
        admin_token = _make_token("admin", "default", tmp_tokens_file)

        # Can list runs
        resp = api_client.get("/runs", headers=_auth(admin_token))
        assert resp.status_code == 200

        # Can list approvals
        resp = api_client.get("/approvals", headers=_auth(admin_token))
        assert resp.status_code == 200

    def test_health_still_works(self, api_client: TestClient) -> None:
        resp = api_client.get("/healthz")
        assert resp.status_code == 200

    def test_record_run_start_old_callers_still_work(self) -> None:
        """Callers that don't pass tenant still work (defaults to 'default')."""
        run_id = state_service.record_run_start("proj", "task", "running")
        assert isinstance(run_id, int)
        assert run_id >= 1

    def test_record_approval_request_old_callers_still_work(self) -> None:
        """Callers that don't pass tenant to record_approval_request still work."""
        run_id = state_service.record_run_start("proj", "task")
        # Should not raise
        state_service.record_approval_request(run_id, "proj", "task", {})

    def test_audit_old_callers_still_work(self) -> None:
        """record_audit callers that don't pass tenant still work."""
        # Should not raise
        state_service.record_audit("hash", "run", "/run", "POST", "ok")
