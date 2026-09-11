"""Tests for `POST /v1/roles/draft` — Agent Studio Phase 3 (HP-27).

The endpoint is a thin, fail-closed wrapper over
`role_draft_service.draft_role`: admin-gated, proposal-only (never writes
the store), empty spec is 400, LLM failure is 502. Mirrors the auth
fixtures in `tests/test_roles_api.py` / `tests/test_concierge_endpoint.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivepilot.services.role_draft_service import RoleDraftError, RoleDraftResult
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


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def _draft_fields(**over) -> dict:
    body = {
        "name": "tf_auditor",
        "title": "Terraform Security Auditor",
        "model_profile": "architecture",
        "inputs": ["terraform"],
        "outputs": ["security_report"],
        "can_block": True,
        "order": 6,
        "runner": "claude",
        "prompt_text": "Review Terraform for security defects.",
    }
    body.update(over)
    return body


class TestRolesDraftEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.post("/v1/roles/draft", json={"spec": "an auditor"})
        assert resp.status_code == 401

    def test_requires_admin(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.post("/v1/roles/draft", json={"spec": "an auditor"}, headers=_auth(raw))
        assert resp.status_code == 403

    def test_empty_spec_is_400(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/roles/draft", json={"spec": "  "}, headers=_auth(raw))
        assert resp.status_code == 400
        assert "spec" in resp.json()["detail"]

    def test_returns_proposal_not_saved(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import role_draft_service, state_service

        monkeypatch.setattr(
            role_draft_service,
            "draft_role",
            lambda spec: RoleDraftResult(fields=_draft_fields(), lint=[], notes=[], saved=False),
        )
        raw, _ = add_token("admin")
        resp = api_client.post(
            "/v1/roles/draft",
            json={"spec": "a security auditor that reviews Terraform, can block release"},
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved"] is False
        assert body["draft"]["name"] == "tf_auditor"
        assert body["draft"]["can_block"] is True
        assert body["draft"]["runner"] == "claude"
        assert body["lint"] == []
        assert state_service.get_role_row("tf_auditor") is None

    def test_llm_failure_is_502(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import role_draft_service

        def _boom(spec: str):
            raise RoleDraftError("the authoring model did not return a draft")

        monkeypatch.setattr(role_draft_service, "draft_role", _boom)
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/roles/draft", json={"spec": "an auditor"}, headers=_auth(raw))
        assert resp.status_code == 502
        assert "did not return" in resp.json()["detail"]

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.services import role_draft_service

        monkeypatch.setattr(
            role_draft_service,
            "draft_role",
            lambda spec: RoleDraftResult(fields=_draft_fields(), lint=[], notes=[]),
        )
        raw, _ = add_token("admin")
        resp = api_client.post("/roles/draft", json={"spec": "an auditor"}, headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["saved"] is False
