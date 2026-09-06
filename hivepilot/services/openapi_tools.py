"""HP-58: OpenAPI / Swagger document → typed tools."""

from __future__ import annotations

import json
from typing import Any

import yaml

from hivepilot.services import credential_box, state_service
from hivepilot.services.ssrf import SsrfError, fetch_allowed
from hivepilot.services.typed_tools import TypedTool, replace_source_tools, safe_token

_METHODS = {"get", "post", "put", "patch", "delete"}


class OpenApiImportError(ValueError):
    """The blob is not an OpenAPI or Swagger document."""


def parse_spec(text: str) -> dict[str, Any]:
    blob = (text or "").strip()
    if not blob:
        raise OpenApiImportError("empty OpenAPI document")
    data: Any
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = yaml.safe_load(blob)
    if not isinstance(data, dict):
        raise OpenApiImportError("OpenAPI document must be an object")
    if not (data.get("openapi") or data.get("swagger")):
        raise OpenApiImportError("document is missing openapi/swagger version")
    return data


def fetch_spec(url: str, *, fetch=None) -> dict[str, Any]:
    do_fetch = fetch or fetch_allowed
    try:
        raw = do_fetch(url)
    except SsrfError as exc:
        raise OpenApiImportError(str(exc)) from exc
    return parse_spec(raw.decode("utf-8", errors="replace"))


def tools_from_spec(spec: dict[str, Any], *, source_id: str) -> list[TypedTool]:
    title = str((spec.get("info") or {}).get("title") or source_id)
    source = safe_token(source_id or title, fallback="openapi")
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        raise OpenApiImportError("paths must be an object")
    tools: list[TypedTool] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _METHODS or not isinstance(op, dict):
                continue
            local = str(op.get("operationId") or f"{method}_{path}")
            schema = _input_schema(op)
            tools.append(
                TypedTool(
                    qualified_name="",
                    local_name=local,
                    source_kind="openapi",
                    source_id=source,
                    description=str(op.get("summary") or op.get("description") or ""),
                    input_schema=schema,
                )
            )
    if not tools:
        raise OpenApiImportError("OpenAPI document has no operations")
    return tools


def _input_schema(op: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in op.get("parameters") or []:
        if not isinstance(param, dict) or not param.get("name"):
            continue
        name = str(param["name"])
        properties[name] = param.get("schema") or {"type": "string"}
        if param.get("required"):
            required.append(name)
    body = op.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content") or {}
        if isinstance(content, dict):
            json_body = content.get("application/json") or next(iter(content.values()), None)
            if isinstance(json_body, dict) and isinstance(json_body.get("schema"), dict):
                properties["body"] = json_body["schema"]
                if body.get("required"):
                    required.append("body")
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def import_openapi(
    *,
    text: str | None = None,
    url: str | None = None,
    name: str | None = None,
    credentials: dict[str, str] | None = None,
    fetch=None,
) -> dict[str, Any]:
    """Store an OpenAPI source and its typed tools. Never returns plaintext creds."""
    import uuid
    from urllib.parse import urlparse

    spec: dict[str, Any]
    if url:
        spec = fetch_spec(url, fetch=fetch)
        source_name = safe_token(name or urlparse(url).hostname or "openapi")
    elif text:
        spec = parse_spec(text)
        title = str((spec.get("info") or {}).get("title") or "openapi")
        source_name = safe_token(name or title)
    else:
        raise OpenApiImportError("provide text or url")

    ciphertext = None
    if credentials:
        if not credential_box.can_encrypt():
            raise OpenApiImportError(
                "literal credentials require HIVEPILOT_CREDENTIALS_KEY; use ${env:} refs instead"
            )
        ciphertext = credential_box.encrypt_secret_map(credentials)

    source_id = uuid.uuid4().hex
    source = state_service.insert_openapi_source(
        source_id=source_id,
        name=source_name,
        url=url,
        credentials_ciphertext=ciphertext,
    )
    tools = replace_source_tools("openapi", source_id, tools_from_spec(spec, source_id=source_name))
    return {
        "source": {
            "id": source["id"],
            "name": source["name"],
            "url": source.get("url"),
            "has_credentials": bool(ciphertext),
        },
        "tools": [tool.to_dict() for tool in tools],
    }
