"""MCP server registry + paste-anything import (HP-76).

A first-class list of MCP servers the operator has added — separate from
Claude's ephemeral ``--mcp-config`` JSON and from the plugin catalog. The
command center (Pollen ``McpView``) reads this registry.

**Paste-anything** (Hermes « colle n'importe quoi ») accepts:

- Claude/Cursor ``{"mcpServers": {name: {command, args, url, env}}}`` JSON
- a bare ``https://…`` URL (stored as remote HTTP; never fetched — SSRF)
- a shell command (``npx -y @modelcontextprotocol/server-filesystem …``)

Literal env values are **stripped** on import (only ``${env:NAME}`` refs
are kept) so a pasted config cannot persist a secret in ``state.db``.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from hivepilot.services import state_service

_ENV_REF = re.compile(r"^\$\{env:[A-Za-z_][A-Za-z0-9_]*\}$")
_SAFE_NAME = re.compile(r"[^a-z0-9._-]+")


@dataclass
class McpServerDraft:
    name: str
    transport: str  # "stdio" | "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    source: str = "import"
    stripped_env_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class McpCatalogEntry:
    name: str
    description: str
    transport: str
    command: str | None
    args: tuple[str, ...]
    url: str | None
    paste: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "paste": self.paste,
        }

    def as_draft(self) -> McpServerDraft:
        return McpServerDraft(
            name=self.name,
            transport=self.transport,
            command=self.command,
            args=list(self.args),
            url=self.url,
            source="catalog",
        )


#: Curated templates an operator can one-click add. Metadata only — adding
#: one writes a registry row; it does not install npm/pip or fetch a URL.
CATALOG: tuple[McpCatalogEntry, ...] = (
    McpCatalogEntry(
        name="filesystem",
        description="Local filesystem tools (read/write under a root you pass).",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem"),
        url=None,
        paste="npx -y @modelcontextprotocol/server-filesystem /path/to/root",
    ),
    McpCatalogEntry(
        name="github",
        description="GitHub issues, PRs, and repos. Token via ${env:GITHUB_TOKEN}.",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        url=None,
        paste='{"mcpServers":{"github":{"command":"npx","args":["-y","@modelcontextprotocol/server-github"],"env":{"GITHUB_TOKEN":"${env:GITHUB_TOKEN}"}}}}',
    ),
    McpCatalogEntry(
        name="fetch",
        description="HTTP fetch tool for the agent (stdio).",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-fetch"),
        url=None,
        paste="npx -y @modelcontextprotocol/server-fetch",
    ),
    McpCatalogEntry(
        name="memory",
        description="Simple knowledge-graph memory server.",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
        url=None,
        paste="npx -y @modelcontextprotocol/server-memory",
    ),
    McpCatalogEntry(
        name="token-savior",
        description="HivePilot's bundled recall MCP (already a plugin — add only if you want it listed here).",
        transport="stdio",
        command="token-savior",
        args=(),
        url=None,
        paste="token-savior",
    ),
)


class McpImportError(ValueError):
    """The pasted blob could not be turned into any server draft."""


def _safe_name(raw: str, fallback: str = "server") -> str:
    cleaned = _SAFE_NAME.sub("-", (raw or "").strip().lower()).strip("-._")
    return (cleaned or fallback)[:64]


def _safe_env(raw: Any) -> tuple[dict[str, str], list[str]]:
    """Keep ``${env:NAME}`` refs only. Literal values are dropped, never stored."""
    kept: dict[str, str] = {}
    stripped: list[str] = []
    if not isinstance(raw, dict):
        return kept, stripped
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if _ENV_REF.fullmatch(value):
            kept[key] = value
        else:
            stripped.append(key)
    return kept, stripped


def _draft_from_stanza(name: str, stanza: dict[str, Any], *, source: str) -> McpServerDraft:
    env, stripped = _safe_env(stanza.get("env"))
    url = stanza.get("url") or stanza.get("serverUrl")
    command = stanza.get("command")
    args = stanza.get("args") or []
    if not isinstance(args, list):
        args = []
    args = [str(a) for a in args]
    if url:
        return McpServerDraft(
            name=_safe_name(name),
            transport="http",
            url=str(url).strip(),
            env=env,
            source=source,
            stripped_env_keys=stripped,
        )
    if command:
        return McpServerDraft(
            name=_safe_name(name),
            transport="stdio",
            command=str(command).strip(),
            args=args,
            env=env,
            source=source,
            stripped_env_keys=stripped,
        )
    raise McpImportError(f"server '{name}' has neither command nor url")


def parse_import(text: str) -> list[McpServerDraft]:
    """Turn a pasted blob into one or more drafts. Never fetches a URL."""
    blob = (text or "").strip()
    if not blob:
        raise McpImportError("empty paste")

    if blob[0] in "{[":
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise McpImportError(f"invalid JSON: {exc}") from exc
        return _parse_json(data)

    if blob.startswith(("http://", "https://")):
        parsed = urlparse(blob.split()[0])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise McpImportError("URL must be http(s) with a host")
        return [
            McpServerDraft(
                name=_safe_name(parsed.hostname or "remote"),
                transport="http",
                url=blob.split()[0],
                source="import",
            )
        ]

    try:
        parts = shlex.split(blob)
    except ValueError as exc:
        raise McpImportError(f"could not parse command: {exc}") from exc
    if not parts:
        raise McpImportError("empty command")
    name = _name_from_command(parts)
    return [
        McpServerDraft(
            name=name,
            transport="stdio",
            command=parts[0],
            args=parts[1:],
            source="import",
        )
    ]


def _name_from_command(parts: list[str]) -> str:
    for token in reversed(parts):
        if token.startswith("-") or token.startswith("/") or token.startswith("."):
            continue
        # npm-style package: @scope/server-github → github
        leaf = token.rsplit("/", 1)[-1]
        leaf = leaf.removeprefix("server-").removeprefix("mcp-server-")
        if leaf and leaf not in {".", ".."}:
            return _safe_name(leaf)
    return _safe_name(parts[0])


def _parse_json(data: Any) -> list[McpServerDraft]:
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        drafts = [
            _draft_from_stanza(name, stanza, source="import")
            for name, stanza in data["mcpServers"].items()
            if isinstance(stanza, dict)
        ]
        if not drafts:
            raise McpImportError("mcpServers is empty")
        return drafts
    if isinstance(data, dict) and (data.get("command") or data.get("url")):
        return [_draft_from_stanza(str(data.get("name") or "server"), data, source="import")]
    if isinstance(data, list):
        drafts = [
            _draft_from_stanza(str(item.get("name") or f"server-{i}"), item, source="import")
            for i, item in enumerate(data)
            if isinstance(item, dict)
        ]
        if drafts:
            return drafts
    raise McpImportError("JSON is not an mcpServers map, a server object, or a list of servers")


def catalog() -> list[dict[str, Any]]:
    installed = {row["name"] for row in state_service.list_mcp_servers()}
    return [{**entry.to_dict(), "installed": entry.name in installed} for entry in CATALOG]


def add_draft(draft: McpServerDraft) -> dict[str, Any]:
    return state_service.upsert_mcp_server(
        name=draft.name,
        transport=draft.transport,
        command=draft.command,
        args=draft.args,
        url=draft.url,
        env=draft.env,
        source=draft.source,
    )


def add_from_catalog(name: str) -> dict[str, Any]:
    for entry in CATALOG:
        if entry.name == name:
            return add_draft(entry.as_draft())
    raise KeyError(name)


def import_and_save(text: str) -> dict[str, Any]:
    drafts = parse_import(text)
    servers = [add_draft(d) for d in drafts]
    stripped = sorted({k for d in drafts for k in d.stripped_env_keys})
    return {
        "drafts": [d.to_dict() for d in drafts],
        "servers": servers,
        "stripped_env_keys": stripped,
    }
