"""HP-59: Composio + Pipedream catalog sync — no execute, no fake connect."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import managed_catalogs, typed_tools
from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_refuses_without_keys(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "composio_api_key", None)
    monkeypatch.setattr(settings, "pipedream_client_id", None)
    monkeypatch.setattr(settings, "pipedream_client_secret", None)
    monkeypatch.setattr(settings, "pipedream_project_id", None)
    assert managed_catalogs.public_status() == {
        "composio": {"configured": False},
        "pipedream": {"configured": False},
    }
    with pytest.raises(managed_catalogs.ManagedCatalogError, match="COMPOSIO_API_KEY"):
        managed_catalogs.sync_composio(toolkit="github")
    with pytest.raises(managed_catalogs.ManagedCatalogError, match="PIPEDREAM"):
        managed_catalogs.sync_pipedream(app="slack")


def test_composio_sync_maps_tools(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "composio_api_key", "ck_test")

    def _fetch(url: str, **_kwargs):
        assert "backend.composio.dev" in url
        assert "toolkit_slug=github" in url
        return json.dumps(
            {
                "items": [
                    {
                        "slug": "GITHUB_CREATE_ISSUE",
                        "name": "Create Issue",
                        "description": "Open an issue",
                        "input_parameters": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                        },
                    }
                ]
            }
        ).encode()

    tools = managed_catalogs.sync_composio(toolkit="github", fetch=_fetch)
    assert [tool.qualified_name for tool in tools] == ["composio__github__github_create_issue"]
    listed = typed_tools.list_typed_tools(source_kind="composio", source_id="github")
    assert listed[0].local_name == "github_create_issue"


def test_composio_refuses_foreign_host(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "composio_api_key", "ck_test")
    with pytest.raises(managed_catalogs.ManagedCatalogError, match="toolkit is required"):
        managed_catalogs.sync_composio(toolkit="  ")


def test_pipedream_sync_maps_actions(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "pipedream_client_id", "cid")
    monkeypatch.setattr(settings, "pipedream_client_secret", "csec")
    monkeypatch.setattr(settings, "pipedream_project_id", "proj_abc")
    monkeypatch.setattr(settings, "pipedream_environment", "development")

    def _fetch(url: str, **kwargs):
        if url.endswith("/v1/oauth/token"):
            assert kwargs.get("method") == "POST"
            return json.dumps({"access_token": "pd_tok"}).encode()
        assert "api.pipedream.com/v1/connect/proj_abc/components" in url
        assert "app=slack" in url
        assert kwargs.get("headers", {}).get("Authorization") == "Bearer pd_tok"
        return json.dumps(
            {
                "data": [
                    {
                        "key": "slack-send-message",
                        "name": "Send Message",
                        "description": "Post to a channel",
                        "configurable_props": [
                            {"name": "channel", "type": "string", "optional": False},
                            {"name": "text", "type": "string", "optional": True},
                        ],
                    }
                ]
            }
        ).encode()

    tools = managed_catalogs.sync_pipedream(app="slack", fetch=_fetch)
    assert tools[0].qualified_name == "pipedream__slack__slack-send-message"
    assert tools[0].input_schema["required"] == ["channel"]


def test_api_sync_role_gates_and_catalog(tmp_tokens_file, api_client, monkeypatch):
    from hivepilot.config import settings
    from hivepilot.services import managed_catalogs as catalogs

    monkeypatch.setattr(settings, "composio_api_key", "ck_test")
    monkeypatch.setattr(settings, "pipedream_client_id", None)
    monkeypatch.setattr(settings, "pipedream_client_secret", None)
    monkeypatch.setattr(settings, "pipedream_project_id", None)

    def _fetch(url: str, **_kwargs):
        return json.dumps(
            {"items": [{"slug": "GMAIL_SEND_EMAIL", "description": "Send", "input_parameters": {}}]}
        ).encode()

    monkeypatch.setattr(catalogs, "ssrf", catalogs.ssrf)
    monkeypatch.setattr(catalogs.ssrf, "fetch_allowed", _fetch)

    admin, _ = add_token("admin", note="hp59")
    reader, _ = add_token("read", note="hp59-read")

    status = api_client.get("/v1/tools/managed", headers=_auth(reader))
    assert status.status_code == 200
    assert status.json()["composio"]["configured"] is True
    assert status.json()["pipedream"]["configured"] is False
    assert "api_key" not in json.dumps(status.json())

    denied = api_client.post(
        "/v1/tools/composio/sync", headers=_auth(reader), json={"toolkit": "gmail"}
    )
    assert denied.status_code == 403

    ok = api_client.post("/v1/tools/composio/sync", headers=_auth(admin), json={"toolkit": "gmail"})
    assert ok.status_code == 200
    names = [row["qualified_name"] for row in ok.json()["tools"]]
    assert names == ["composio__gmail__gmail_send_email"]

    missing = api_client.post(
        "/v1/tools/pipedream/sync", headers=_auth(admin), json={"app": "slack"}
    )
    assert missing.status_code == 400
    assert "PIPEDREAM" in missing.json()["detail"]
