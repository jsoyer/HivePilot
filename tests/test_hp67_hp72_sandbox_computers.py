"""HP-67 / HP-72 — no-go provider + fail-closed take-over.

Honesty: no invented sandboxes, no host-desktop take-over.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import sandbox_computers
from hivepilot.services.token_service import add_token


def test_provider_snapshot_is_nogo() -> None:
    snap = sandbox_computers.provider_snapshot()
    assert snap["configured"] is False
    assert snap["provider"] is None
    assert snap["decision"] == "no-go"
    assert snap["evaluated"] == ["docker", "e2b", "daytona", "box"]
    assert "sandbox" not in snap
    assert "servers" not in snap


def test_session_snapshot_is_detached() -> None:
    snap = sandbox_computers.session_snapshot()
    assert snap["attached"] is False
    assert snap["can_takeover"] is False
    assert snap["controller"] is None


@pytest.mark.parametrize("action", ["takeover", "handback", "skip"])
def test_control_refuses_without_desktop(action: str) -> None:
    result = sandbox_computers.control(action)
    assert result["ok"] is False
    assert result["action"] == action
    assert result["attached"] is False
    assert "refused" in result["error"]


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


def test_provider_endpoint_read_role(tmp_tokens_file, api_client) -> None:
    raw, _ = add_token("read")
    resp = api_client.get("/v1/sandbox/provider", headers=_auth(raw))
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"configured", "provider", "decision", "evaluated", "note"}
    assert data["decision"] == "no-go"


def test_session_endpoint_read_role(tmp_tokens_file, api_client) -> None:
    raw, _ = add_token("read")
    resp = api_client.get("/v1/computer/session", headers=_auth(raw))
    assert resp.status_code == 200
    assert resp.json()["can_takeover"] is False


def test_takeover_requires_approve(tmp_tokens_file, api_client) -> None:
    raw, _ = add_token("read")
    resp = api_client.post("/v1/computer/takeover", headers=_auth(raw))
    assert resp.status_code == 403


def test_takeover_refuses_when_approve(tmp_tokens_file, api_client) -> None:
    raw, _ = add_token("approve")
    resp = api_client.post("/v1/computer/takeover", headers=_auth(raw))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["action"] == "takeover"


@pytest.mark.parametrize("path", ["/v1/computer/handback", "/v1/computer/skip"])
def test_handback_and_skip_refuse(tmp_tokens_file, api_client, path: str) -> None:
    raw, _ = add_token("approve")
    resp = api_client.post(path, headers=_auth(raw))
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_provider_rejects_anonymous(api_client) -> None:
    assert api_client.get("/v1/sandbox/provider").status_code in {401, 403}
