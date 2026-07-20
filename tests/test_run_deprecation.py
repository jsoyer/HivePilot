"""
Tests for the `POST /run` deprecation (roadmap polish item 1).

`POST /run` (dual-registered on `/run` and `/v1/run`) is the synchronous
predecessor to `POST /v1/runs` (async, see `test_async_runs_endpoint.py`).
This is a docs-visible + runtime-visible, zero-behavior-change deprecation:

1. `/run` still works exactly as before (same response shape, same status
   code) -- this is NOT a removal.
2. `/run` now surfaces `Deprecation: true` and
   `Link: </v1/runs>; rel="successor-version"` response headers (RFC 8594).
3. `/run`'s OpenAPI schema entry is marked `deprecated: true`.
4. `POST /v1/runs` (the successor) is NOT marked deprecated and does NOT
   carry the `Deprecation` header.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token


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


def _fake_orchestrator() -> SimpleNamespace:
    return SimpleNamespace(run_task=lambda **kwargs: [])


class TestRunEndpointDeprecation:
    def test_run_still_works_and_returns_deprecation_headers(
        self, api_client: TestClient, tmp_tokens_file, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.services import api_service

        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: _fake_orchestrator())

        raw, _ = add_token("run")
        resp = api_client.post(
            "/run", json={"task": "deploy", "projects": ["p"]}, headers=_auth(raw)
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"results": []}
        assert resp.headers.get("Deprecation") == "true"
        assert resp.headers.get("Link") == '</v1/runs>; rel="successor-version"'

    def test_v1_run_alias_also_returns_deprecation_headers(
        self, api_client: TestClient, tmp_tokens_file, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.services import api_service

        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: _fake_orchestrator())

        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/run", json={"task": "deploy", "projects": ["p"]}, headers=_auth(raw)
        )

        assert resp.status_code == 200, resp.text
        assert resp.headers.get("Deprecation") == "true"
        assert resp.headers.get("Link") == '</v1/runs>; rel="successor-version"'

    def test_openapi_schema_marks_run_deprecated(self, api_client: TestClient) -> None:
        from hivepilot.services.api_service import app

        schema = app.openapi()
        run_post = schema["paths"]["/run"]["post"]
        assert run_post.get("deprecated") is True

    def test_v1_runs_post_is_not_deprecated(self) -> None:
        from hivepilot.services.api_service import app

        schema = app.openapi()
        create_run_post = schema["paths"]["/v1/runs"]["post"]
        assert create_run_post.get("deprecated") is not True

    def test_v1_runs_response_has_no_deprecation_header(
        self, api_client: TestClient, tmp_tokens_file, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pathlib import Path

        from hivepilot.models import ProjectConfig, TaskConfig
        from hivepilot.services import api_service, policy_service

        project = ProjectConfig(path=Path("acme-web"))
        task = TaskConfig(description="deploy things")
        orch = SimpleNamespace(
            tasks=SimpleNamespace(tasks={"deploy": task}),
            _project=lambda name: project,
            _cve_gate_block_detail=lambda *a, **k: None,
            _execute_task=lambda **kwargs: "stub run output",
        )
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: orch)
        monkeypatch.setattr(
            policy_service, "enforce_policy", lambda *a, **k: policy_service.Policy()
        )

        raw, _ = add_token("run")
        client = TestClient(api_service.app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/runs", json={"task": "deploy", "project": "acme-web"}, headers=_auth(raw)
        )
        assert resp.status_code == 202, resp.text
        assert "Deprecation" not in resp.headers
