"""Tests for `GET /v1/whoami` (+ unversioned twin) — Mirador actionable
dashboard PRD, Sprint 1. Lets the calling token introspect its own RBAC
role/tenant so the web client can fail-closed gate action controls
(`useRole()` in `web/src/lib/role-context.tsx`).

Mirrors the auth/fixture patterns established for `GET /v1/plugins/health`
in `tests/test_api_service.py` (`TestPluginsHealthEndpoint`) and
`tests/test_panels_api.py`.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services.token_service import ROLE_RANKS, add_token


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


class TestWhoamiEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/whoami")
        assert resp.status_code == 401

    def test_missing_bearer_prefix_is_401(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        # Header present but not in the `Bearer <token>` shape require_role expects.
        resp = api_client.get("/v1/whoami", headers={"Authorization": raw})
        assert resp.status_code == 401

    def test_invalid_token_is_401(self, api_client, tmp_tokens_file):
        resp = api_client.get("/v1/whoami", headers=_auth("not-a-real-token"))
        assert resp.status_code == 401

    @pytest.mark.parametrize("role", sorted(ROLE_RANKS, key=lambda r: ROLE_RANKS[r]))
    def test_returns_role_and_tenant_for_every_rank(self, api_client, tmp_tokens_file, role):
        """Every rank (read/run/approve/admin) round-trips through whoami —
        the endpoint only requires the floor ("read"), so higher-ranked
        tokens must succeed too, not just the minimum."""
        raw, _ = add_token(role, tenant="acme")
        resp = api_client.get("/v1/whoami", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"role": role, "tenant": "acme"}

    def test_defaults_to_the_default_tenant(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/whoami", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"role": "read", "tenant": "default"}

    def test_response_has_no_extra_fields(self, api_client, tmp_tokens_file):
        """Only {role, tenant} — never the token hash, note, or expiry."""
        raw, _ = add_token("admin", note="do-not-leak-me")
        resp = api_client.get("/v1/whoami", headers=_auth(raw))
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"role", "tenant"}
        assert "do-not-leak-me" not in resp.text
        assert raw not in resp.text

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/whoami", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"role": "read", "tenant": "default"}
