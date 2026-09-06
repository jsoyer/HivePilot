"""HP-58: SSRF guard, credential box, OpenAPI/MCP typed tools, de-collision."""

from __future__ import annotations

import json
import socket
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import (
    credential_box,
    mcp_registry,
    mcp_sync,
    openapi_tools,
    ssrf,
    typed_tools,
)
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


def test_ssrf_allows_loopback_and_blocks_metadata(monkeypatch):
    ssrf.assert_url_allowed("http://127.0.0.1:9/health")
    with pytest.raises(ssrf.SsrfError, match="userinfo"):
        ssrf.assert_url_allowed("https://user:pass@example.com/x")
    with pytest.raises(ssrf.SsrfError, match="https"):
        ssrf.assert_url_allowed("http://example.com/x")

    def _meta(*_a, **_k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _meta)
    with pytest.raises(ssrf.SsrfError, match="private or metadata"):
        ssrf.assert_url_allowed("https://evil.example/latest")


def test_ssrf_allows_public_https(monkeypatch):
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 443))],
    )
    ssrf.assert_url_allowed("https://example.com/openapi.json")


def test_ssrf_blocks_ipv4_mapped_metadata(monkeypatch):
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::ffff:169.254.169.254", 443, 0, 0))
        ],
    )
    with pytest.raises(ssrf.SsrfError, match="private or metadata"):
        ssrf.assert_url_allowed("https://evil.example/latest")


def test_credential_box_roundtrip(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "credentials_key", "hp58-test-key")
    token = credential_box.encrypt_secret_map({"authorization": "Bearer secret"})
    assert "secret" not in token
    assert credential_box.decrypt_secret_map(token)["authorization"] == "Bearer secret"


def test_credential_box_requires_key(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "credentials_key", None)
    assert credential_box.can_encrypt() is False
    with pytest.raises(credential_box.CredentialBoxError):
        credential_box.encrypt_secret_map({"token": "x"})


def test_openapi_tools_from_spec():
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Billing"},
        "paths": {
            "/invoices": {
                "post": {
                    "operationId": "createInvoice",
                    "summary": "Create an invoice",
                    "parameters": [
                        {"name": "dry_run", "in": "query", "schema": {"type": "boolean"}}
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                }
            }
        },
    }
    tools = openapi_tools.tools_from_spec(spec, source_id="billing")
    assert len(tools) == 1
    assert tools[0].local_name == "createInvoice"
    assert "body" in tools[0].input_schema["properties"]


def test_openapi_import_and_get_tools():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Pets"},
        "paths": {"/pets": {"get": {"operationId": "listPets", "summary": "List pets"}}},
    }
    result = openapi_tools.import_openapi(text=json.dumps(spec), name="pets")
    assert result["source"]["name"] == "pets"
    assert result["source"]["has_credentials"] is False
    assert result["tools"][0]["qualified_name"] == "openapi__pets__listpets"
    listed = typed_tools.list_typed_tools(source_kind="openapi")
    assert listed[0].qualified_name == "openapi__pets__listpets"


def test_mcp_sync_writes_namespaced_tools():
    row = mcp_registry.add_draft(
        mcp_registry.McpServerDraft(
            name="github",
            transport="http",
            url="https://mcp.example.com/sse",
            source="import",
        )
    )

    def _fetch(*_a, **_k):
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "create_issue",
                            "description": "Open a GitHub issue",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"title": {"type": "string"}},
                            },
                        }
                    ]
                },
            }
        ).encode()

    tools = mcp_sync.sync_mcp_server(row["id"], fetch=_fetch)
    assert tools[0].qualified_name == "mcp__github__create_issue"
    assert tools[0].source_id == str(row["id"])


def test_tool_name_decollision_keeps_both_sources():
    first = typed_tools.replace_source_tools(
        "mcp",
        "1",
        [
            typed_tools.TypedTool(
                qualified_name="",
                local_name="ping",
                source_kind="mcp",
                source_id="alpha",
                description="",
                input_schema={},
            )
        ],
    )
    second = typed_tools.replace_source_tools(
        "mcp",
        "2",
        [
            typed_tools.TypedTool(
                qualified_name="",
                local_name="ping",
                source_kind="mcp",
                source_id="alpha",
                description="",
                input_schema={},
            )
        ],
    )
    assert first[0].qualified_name == "mcp__alpha__ping"
    assert second[0].qualified_name == "mcp__alpha__ping__2"


def test_mcp_import_encrypts_literal_headers(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "credentials_key", "hp58-mcp-key")
    blob = json.dumps(
        {
            "mcpServers": {
                "remote": {
                    "url": "https://mcp.example.com/sse",
                    "headers": {"Authorization": "Bearer super-secret"},
                    "env": {"LEAK": "sk-literal", "OK": "${env:GITHUB_TOKEN}"},
                }
            }
        }
    )
    result = mcp_registry.import_and_save(blob)
    draft = result["drafts"][0]
    assert "literal_secrets" not in draft
    assert "super-secret" not in json.dumps(result)
    assert draft["env"] == {"OK": "${env:GITHUB_TOKEN}"}
    assert "LEAK" in result["stripped_env_keys"]
    row = result["servers"][0]
    assert row.get("has_credentials") or row.get("credentials_ciphertext")
    secrets = credential_box.decrypt_secret_map(row["credentials_ciphertext"])
    assert secrets["Authorization"] == "Bearer super-secret"
    assert secrets["LEAK"] == "sk-literal"


def test_server_import_does_not_overwrite_existing_name():
    first = mcp_registry.import_and_save("https://mcp.example.com/a")
    second = mcp_registry.import_and_save("https://mcp.example.com/b")
    names = {row["name"] for row in first["servers"] + second["servers"]}
    assert "mcp.example.com" in names
    assert "mcp.example.com-2" in names


def test_mcp_parse_rejects_openapi_json():
    with pytest.raises(mcp_registry.McpImportError, match="OpenAPI"):
        mcp_registry.parse_import(json.dumps({"openapi": "3.1.0", "paths": {}}))


def test_api_sync_and_openapi_import(api_client, tmp_tokens_file, monkeypatch):
    admin, _ = add_token("admin")
    read, _ = add_token("read")
    created = api_client.post(
        "/v1/mcp/import",
        headers=_auth(admin),
        json={"text": "https://mcp.example.com/sse"},
    )
    assert created.status_code == 200
    server_id = created.json()["servers"][0]["id"]
    assert "credentials_ciphertext" not in created.json()["servers"][0]

    monkeypatch.setattr(
        mcp_sync,
        "fetch_allowed",
        lambda *_a, **_k: json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "echo"}]}}
        ).encode(),
    )
    denied = api_client.post(f"/v1/mcp/servers/{server_id}/sync", headers=_auth(read))
    assert denied.status_code == 403
    synced = api_client.post(f"/v1/mcp/servers/{server_id}/sync", headers=_auth(admin))
    assert synced.status_code == 200, synced.text
    assert synced.json()["tools"][0]["local_name"] == "echo"

    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Demo"},
        "paths": {"/ping": {"get": {"operationId": "ping"}}},
    }
    imported = api_client.post(
        "/v1/tools/openapi/import",
        headers=_auth(admin),
        json={"text": json.dumps(spec), "name": "demo"},
    )
    assert imported.status_code == 200, imported.text
    listed = api_client.get("/v1/tools", headers=_auth(read))
    assert listed.status_code == 200
    names = {row["qualified_name"] for row in listed.json()["tools"]}
    assert any(name.endswith("__echo") for name in names)
    assert "openapi__demo__ping" in names

    from hivepilot.config import settings

    monkeypatch.setattr(settings, "credentials_key", "hp58-api-key")
    creds = api_client.post(
        f"/v1/mcp/servers/{server_id}/credentials",
        headers=_auth(admin),
        json={"credentials": {"authorization": "Bearer stored"}},
    )
    assert creds.status_code == 200, creds.text
    assert creds.json()["server"]["has_credentials"] is True
    assert "credentials_ciphertext" not in creds.json()["server"]
    assert "stored" not in creds.text


def test_api_openapi_url_ssrf_denied(api_client, tmp_tokens_file, monkeypatch):
    admin, _ = add_token("admin")
    monkeypatch.setattr(
        openapi_tools,
        "fetch_allowed",
        MagicMock(side_effect=ssrf.SsrfError("refusing to fetch private or metadata address")),
    )
    resp = api_client.post(
        "/v1/tools/openapi/import",
        headers=_auth(admin),
        json={"url": "https://169.254.169.254/latest"},
    )
    assert resp.status_code == 400
    assert "private" in resp.json()["detail"]


def test_api_openapi_credentials_require_key(api_client, tmp_tokens_file, monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "credentials_key", None)
    admin, _ = add_token("admin")
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Locked"},
        "paths": {"/x": {"get": {"operationId": "x"}}},
    }
    resp = api_client.post(
        "/v1/tools/openapi/import",
        headers=_auth(admin),
        json={
            "text": json.dumps(spec),
            "name": "locked",
            "credentials": {"authorization": "Bearer nope"},
        },
    )
    assert resp.status_code == 400
    assert "CREDENTIALS_KEY" in resp.json()["detail"]
