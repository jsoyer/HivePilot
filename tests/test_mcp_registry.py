"""HP-76 — paste-anything import, catalog, probe, and API."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import api_service, mcp_probe, mcp_registry, state_service
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
    return TestClient(api_service.app, raise_server_exceptions=True)


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


class TestParseImport:
    def test_claude_mcp_servers_json(self):
        blob = json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}", "LEAK": "sk-secret"},
                    }
                }
            }
        )
        drafts = mcp_registry.parse_import(blob)
        assert len(drafts) == 1
        d = drafts[0]
        assert d.name == "github" and d.transport == "stdio" and d.command == "npx"
        assert d.env == {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"}
        assert d.stripped_env_keys == ["LEAK"]

    def test_url_is_stored_not_fetched(self):
        drafts = mcp_registry.parse_import("https://mcp.example.com/sse")
        assert drafts[0].transport == "http"
        assert drafts[0].url == "https://mcp.example.com/sse"
        assert drafts[0].name == "mcp.example.com"

    def test_command_line(self):
        drafts = mcp_registry.parse_import("npx -y @modelcontextprotocol/server-filesystem /tmp")
        assert drafts[0].command == "npx"
        assert drafts[0].name == "filesystem"
        assert "/tmp" in drafts[0].args

    def test_empty_and_garbage_raise(self):
        with pytest.raises(mcp_registry.McpImportError):
            mcp_registry.parse_import("   ")
        with pytest.raises(mcp_registry.McpImportError):
            mcp_registry.parse_import("{not json")


class TestProbe:
    def test_stdio_missing_binary(self):
        result = mcp_probe.probe_server(
            {"transport": "stdio", "command": "definitely-not-a-binary-xyz"}
        )
        assert result.status == "missing"

    def test_stdio_python_is_on_path(self):
        result = mcp_probe.probe_server({"transport": "stdio", "command": "python3"})
        assert result.status == "ok"

    def test_remote_http_is_not_contacted(self):
        result = mcp_probe.probe_server({"transport": "http", "url": "https://evil.example/steal"})
        assert result.status == "remote"
        assert "not probed" in result.detail


class TestStoreAndImport:
    def test_import_and_save_round_trip(self):
        result = mcp_registry.import_and_save("npx -y @modelcontextprotocol/server-fetch")
        assert result["servers"][0]["name"] == "fetch"
        listed = state_service.list_mcp_servers()
        assert any(s["name"] == "fetch" for s in listed)

    def test_catalog_add(self):
        server = mcp_registry.add_from_catalog("memory")
        assert server["name"] == "memory" and server["source"] == "catalog"
        with pytest.raises(KeyError):
            mcp_registry.add_from_catalog("no-such-entry")


class TestMcpApi:
    def test_read_endpoints_require_auth(self, api_client):
        assert api_client.get("/v1/mcp/servers").status_code == 401
        assert api_client.get("/v1/mcp/catalog").status_code == 401

    def test_import_is_admin_only(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read", note="r")
        resp = api_client.post("/v1/mcp/import", json={"text": "npx foo"}, headers=_auth(raw))
        assert resp.status_code == 403

    def test_import_list_probe_delete(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin", note="a")
        headers = _auth(raw)
        imported = api_client.post(
            "/v1/mcp/import",
            json={"text": "npx -y @modelcontextprotocol/server-memory"},
            headers=headers,
        )
        assert imported.status_code == 200
        server_id = imported.json()["servers"][0]["id"]

        listed = api_client.get("/v1/mcp/servers", headers=headers)
        assert listed.status_code == 200
        names = {s["name"] for s in listed.json()["servers"]}
        assert "memory" in names

        catalog = api_client.get("/v1/mcp/catalog", headers=headers)
        assert catalog.status_code == 200
        memory = next(c for c in catalog.json()["catalog"] if c["name"] == "memory")
        assert memory["installed"] is True

        probed = api_client.post(f"/v1/mcp/servers/{server_id}/probe", headers=headers)
        assert probed.status_code == 200
        assert probed.json()["server"]["last_probe_status"] in {"ok", "missing"}

        deleted = api_client.delete(f"/v1/mcp/servers/{server_id}", headers=headers)
        assert deleted.status_code == 200
        assert api_client.get("/v1/mcp/servers", headers=headers).json()["servers"] == []
