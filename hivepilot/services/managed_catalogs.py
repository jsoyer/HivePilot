"""HP-59: Composio + Pipedream Connect as typed-tool catalog sources.

Catalog only — tools are listed into ``typed_tools``, never executed.
Hosts are hardcoded (not user URLs). Missing credentials refuse instead of
inventing a fake connect.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from hivepilot.config import settings
from hivepilot.services import ssrf
from hivepilot.services.typed_tools import TypedTool, replace_source_tools, safe_token

FetchFn = Callable[..., bytes]

COMPOSIO_HOST = "backend.composio.dev"
PIPEDREAM_HOST = "api.pipedream.com"
_MAX_TOOLS = 100


class ManagedCatalogError(ValueError):
    """Vendor catalog sync failed or was refused."""


def composio_configured() -> bool:
    return bool((settings.composio_api_key or "").strip())


def pipedream_configured() -> bool:
    return bool(
        (settings.pipedream_client_id or "").strip()
        and (settings.pipedream_client_secret or "").strip()
        and (settings.pipedream_project_id or "").strip()
    )


def public_status() -> dict[str, Any]:
    return {
        "composio": {"configured": composio_configured()},
        "pipedream": {"configured": pipedream_configured()},
    }


def _assert_vendor_host(url: str, allowed: frozenset[str]) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in allowed:
        raise ManagedCatalogError(f"refusing host {host!r}")


def _request(
    url: str,
    *,
    allowed: frozenset[str],
    fetch: FetchFn | None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> Any:
    _assert_vendor_host(url, allowed)
    raw = (fetch or ssrf.fetch_allowed)(url, method=method, headers=headers or {}, body=body)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedCatalogError("vendor response is not JSON") from exc


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data", "tools", "components"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def sync_composio(*, toolkit: str, fetch: FetchFn | None = None) -> list[TypedTool]:
    if not composio_configured():
        raise ManagedCatalogError("HIVEPILOT_COMPOSIO_API_KEY is not set")
    slug = safe_token(toolkit, fallback="")
    if not slug:
        raise ManagedCatalogError("toolkit is required")
    url = f"https://{COMPOSIO_HOST}/api/v3.1/tools?toolkit_slug={slug}&limit={_MAX_TOOLS}"
    payload = _request(
        url,
        allowed=frozenset({COMPOSIO_HOST}),
        fetch=fetch,
        headers={"x-api-key": settings.composio_api_key or "", "Accept": "application/json"},
    )
    tools: list[TypedTool] = []
    for row in _rows(payload)[:_MAX_TOOLS]:
        local = str(row.get("slug") or row.get("name") or "")
        if not local:
            continue
        schema = row.get("input_parameters") or row.get("input_schema") or {}
        if not isinstance(schema, dict):
            schema = {}
        tools.append(
            TypedTool(
                qualified_name="",
                local_name=local,
                source_kind="composio",
                source_id=slug,
                description=str(row.get("description") or row.get("name") or ""),
                input_schema=schema,
            )
        )
    return replace_source_tools("composio", slug, tools)


def _pipedream_access_token(fetch: FetchFn | None) -> str:
    url = f"https://{PIPEDREAM_HOST}/v1/oauth/token"
    body = json.dumps(
        {
            "grant_type": "client_credentials",
            "client_id": settings.pipedream_client_id,
            "client_secret": settings.pipedream_client_secret,
        }
    ).encode("utf-8")
    payload = _request(
        url,
        allowed=frozenset({PIPEDREAM_HOST}),
        fetch=fetch,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        body=body,
    )
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token or not isinstance(token, str):
        raise ManagedCatalogError("pipedream token response missing access_token")
    return token


def _props_to_schema(props: Any) -> dict[str, Any]:
    if isinstance(props, dict) and props.get("type") == "object":
        return props
    if not isinstance(props, list):
        return {"type": "object", "properties": {}}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for item in props:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("key") or "")
        if not name:
            continue
        properties[name] = {"type": str(item.get("type") or "string")}
        if item.get("optional") is False:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def sync_pipedream(*, app: str, fetch: FetchFn | None = None) -> list[TypedTool]:
    if not pipedream_configured():
        raise ManagedCatalogError(
            "HIVEPILOT_PIPEDREAM_CLIENT_ID, HIVEPILOT_PIPEDREAM_CLIENT_SECRET, "
            "and HIVEPILOT_PIPEDREAM_PROJECT_ID are required"
        )
    slug = safe_token(app, fallback="")
    if not slug:
        raise ManagedCatalogError("app is required")
    token = _pipedream_access_token(fetch)
    project = (settings.pipedream_project_id or "").strip()
    env = (settings.pipedream_environment or "development").strip() or "development"
    url = (
        f"https://{PIPEDREAM_HOST}/v1/connect/{project}/components"
        f"?component_type=action&app={slug}&limit={_MAX_TOOLS}"
    )
    payload = _request(
        url,
        allowed=frozenset({PIPEDREAM_HOST}),
        fetch=fetch,
        headers={
            "Authorization": f"Bearer {token}",
            "x-pd-environment": env,
            "Accept": "application/json",
        },
    )
    tools: list[TypedTool] = []
    for row in _rows(payload)[:_MAX_TOOLS]:
        local = str(row.get("key") or row.get("name") or "")
        if not local:
            continue
        tools.append(
            TypedTool(
                qualified_name="",
                local_name=local,
                source_kind="pipedream",
                source_id=slug,
                description=str(row.get("description") or row.get("name") or ""),
                input_schema=_props_to_schema(row.get("configurable_props")),
            )
        )
    return replace_source_tools("pipedream", slug, tools)
