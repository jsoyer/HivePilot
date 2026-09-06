"""HP-58: list tools from an HTTPS MCP server (JSON-RPC tools/list)."""

from __future__ import annotations

import json
from typing import Any, Callable

from hivepilot.services import credential_box, state_service
from hivepilot.services.ssrf import SsrfError, fetch_allowed
from hivepilot.services.typed_tools import TypedTool, replace_source_tools

FetchFn = Callable[..., bytes]


class McpSyncError(ValueError):
    """The MCP server could not be listed."""


def _headers_for(row: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        secrets = credential_box.decrypt_secret_map(row.get("credentials_ciphertext"))
    except credential_box.CredentialBoxError:
        secrets = {}
    token = secrets.get("authorization") or secrets.get("Authorization") or secrets.get("token")
    if token and not token.startswith("${"):
        headers["Authorization"] = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )
    return headers


def list_remote_tools(row: dict[str, Any], *, fetch: FetchFn | None = None) -> list[TypedTool]:
    do_fetch = fetch or fetch_allowed
    if (row.get("transport") or "") != "http":
        raise McpSyncError("only http MCP servers can be synced")
    url = (row.get("url") or "").strip()
    if not url:
        raise McpSyncError("http server has no url")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        raw = do_fetch(
            url,
            method="POST",
            headers=_headers_for(row),
            body=json.dumps(payload).encode("utf-8"),
        )
    except SsrfError as exc:
        raise McpSyncError(str(exc)) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise McpSyncError("MCP tools/list did not return JSON") from exc
    if data.get("error"):
        raise McpSyncError(str(data["error"]))
    tools = ((data.get("result") or {}).get("tools")) or []
    if not isinstance(tools, list) or not tools:
        raise McpSyncError("MCP tools/list returned no tools")
    source_id = str(row.get("name") or row.get("id") or "mcp")
    out: list[TypedTool] = []
    for item in tools:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        schema = item.get("inputSchema") or item.get("input_schema") or {}
        if not isinstance(schema, dict):
            schema = {}
        out.append(
            TypedTool(
                qualified_name="",
                local_name=str(item["name"]),
                source_kind="mcp",
                source_id=source_id,
                description=str(item.get("description") or ""),
                input_schema=schema,
            )
        )
    if not out:
        raise McpSyncError("MCP tools/list returned no named tools")
    return out


def sync_mcp_server(server_id: int, *, fetch: FetchFn | None = None) -> list[TypedTool]:
    row = state_service.get_mcp_server(server_id)
    if row is None:
        raise McpSyncError(f"unknown server {server_id}")
    tools = list_remote_tools(row, fetch=fetch)
    return replace_source_tools("mcp", str(row["id"]), tools)
