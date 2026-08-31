"""Tests for the store-backed roles CRUD API — Agent Studio Phase 1, slice 3
(HP-25). `GET/POST/PUT/DELETE /v1/roles` are the mutable roster the visual
builder (Phase 2) and NL authoring (Phase 3) drive: read-gated reads,
admin-gated writes, Role-schema validation, a fail-closed governance guardrail
against self-granted `bypassPermissions`, and live application via
`refresh_roles()`.

The autouse `_isolate_state_db` fixture (conftest.py) gives each test an empty
`roles` table; a write seeds the real repo `roles.yaml` first so the store
holds the WHOLE roster before the edit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    import yaml

    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _restore_roles_global():
    """A write endpoint calls `refresh_roles()`, which rebinds the process-wide
    `roles.ROLES`. Snapshot + restore it so these tests never leak an edited
    roster into unrelated tests."""
    from hivepilot import roles

    original = dict(roles.ROLES)
    yield
    roles.ROLES = original


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def _payload(**over) -> dict:
    body = {
        "name": "auditor",
        "title": "Security Auditor",
        "model_profile": "architecture",
        "inputs": [],
        "outputs": ["report"],
        "can_block": True,
        "order": 2,
        "runner": "openai",
        "prompt_text": "You audit the codebase for security defects.",
    }
    body.update(over)
    return body


class TestRolesRead:
    def test_list_requires_auth(self, api_client):
        assert api_client.get("/v1/roles").status_code == 401

    def test_list_returns_yaml_roster_when_store_empty(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/roles", headers=_auth(raw))
        assert resp.status_code == 200
        names = {r["name"] for r in resp.json()["roles"]}
        assert "ceo" in names  # the repo roles.yaml roster
        # a live/YAML role never leaks an absolute prompt path, only a filename
        for role in resp.json()["roles"]:
            if role.get("prompt_file"):
                assert "/" not in role["prompt_file"]

    def test_get_one_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/roles/ceo", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["name"] == "ceo"

    def test_get_unknown_role_404(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        assert api_client.get("/v1/roles/nope", headers=_auth(raw)).status_code == 404


class TestRolesCreate:
    def test_create_requires_admin(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.post("/v1/roles", json=_payload(), headers=_auth(raw))
        assert resp.status_code == 403

    def test_create_persists_and_applies_live(self, api_client, tmp_tokens_file):
        from hivepilot.services import api_service, state_service

        raw, _ = add_token("admin")
        resp = api_client.post("/v1/roles", json=_payload(), headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["name"] == "auditor"
        # stored in the roles table...
        assert state_service.get_role_row("auditor") is not None
        # ...and applied live (refresh_roles ran, so ROLES now has it)
        assert "auditor" in api_service.roles.ROLES
        # ...and readable back through the API
        got = api_client.get("/v1/roles/auditor", headers=_auth(raw))
        assert got.status_code == 200
        assert got.json()["title"] == "Security Auditor"

    def test_create_conflict_on_existing_name(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        # `ceo` exists in the seeded YAML roster -> creating it again is a 409
        resp = api_client.post("/v1/roles", json=_payload(name="ceo"), headers=_auth(raw))
        assert resp.status_code == 409

    def test_create_without_prompt_is_400(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        body = _payload()
        body.pop("prompt_text")
        resp = api_client.post("/v1/roles", json=body, headers=_auth(raw))
        assert resp.status_code == 400
        assert "prompt" in resp.json()["detail"]

    def test_create_invalid_effort_is_400(self, api_client, tmp_tokens_file):
        """`effort` passes the RoleWrite shape (str) but fails Role validation —
        surfaced as a 400, not a 500."""
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/roles", json=_payload(effort="turbo"), headers=_auth(raw))
        assert resp.status_code == 400

    def test_missing_required_field_is_422(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        body = _payload()
        body.pop("title")
        resp = api_client.post("/v1/roles", json=body, headers=_auth(raw))
        assert resp.status_code == 422  # FastAPI body validation


class TestRolesGovernance:
    def test_bypass_permissions_refused_by_default(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.post(
            "/v1/roles",
            json=_payload(permission_mode="bypassPermissions"),
            headers=_auth(raw),
        )
        assert resp.status_code == 403
        assert "bypassPermissions" in resp.json()["detail"]

    def test_bypass_permissions_allowed_when_explicitly_enabled(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "allow_dangerous_role_capabilities", True)
        raw, _ = add_token("admin")
        resp = api_client.post(
            "/v1/roles",
            json=_payload(permission_mode="bypassPermissions"),
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        assert resp.json()["permission_mode"] == "bypassPermissions"


class TestRolesUpdate:
    def test_update_changes_a_field(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        api_client.post("/v1/roles", json=_payload(), headers=_auth(raw))
        resp = api_client.put(
            "/v1/roles/auditor",
            json=_payload(title="Lead Auditor"),
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Lead Auditor"
        got = api_client.get("/v1/roles/auditor", headers=_auth(raw))
        assert got.json()["title"] == "Lead Auditor"

    def test_update_name_mismatch_is_400(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.put("/v1/roles/auditor", json=_payload(name="other"), headers=_auth(raw))
        assert resp.status_code == 400

    def test_update_requires_admin(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.put("/v1/roles/auditor", json=_payload(), headers=_auth(raw))
        assert resp.status_code == 403


class TestRolesDelete:
    def test_delete_removes_and_applies_live(self, api_client, tmp_tokens_file):
        from hivepilot.services import api_service, state_service

        raw, _ = add_token("admin")
        api_client.post("/v1/roles", json=_payload(), headers=_auth(raw))
        assert "auditor" in api_service.roles.ROLES

        resp = api_client.delete("/v1/roles/auditor", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True, "name": "auditor"}
        assert state_service.get_role_row("auditor") is None
        assert "auditor" not in api_service.roles.ROLES

    def test_delete_unknown_reports_not_deleted(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.delete("/v1/roles/nope", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"deleted": False, "name": "nope"}

    def test_delete_requires_admin(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        assert api_client.delete("/v1/roles/auditor", headers=_auth(raw)).status_code == 403


class TestUnversionedRoutes:
    def test_unversioned_list_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        assert api_client.get("/roles", headers=_auth(raw)).status_code == 200
