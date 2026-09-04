"""MCP server health probe (HP-76).

Never raises. Never executes a server command (a probe that *starts* an MCP
process would be a surprise side-effect on a GET). Never fetches a non-loopback
URL (SSRF — HP-58 owns the allowlisted HTTPS import).

- **stdio**: ``shutil.which(command)`` — is the binary on PATH?
- **http** on loopback: cheap GET with a short timeout.
- **http** elsewhere: ``remote`` — recorded, not contacted.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from hivepilot.services import state_service

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass
class McpProbe:
    status: str  # ok | missing | remote | error
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOOPBACK


def probe_server(row: dict[str, Any], *, timeout: float = 1.5) -> McpProbe:
    transport = row.get("transport") or "stdio"
    if transport == "stdio":
        command = (row.get("command") or "").strip()
        if not command:
            return McpProbe(status="error", detail="stdio server has no command")
        path = shutil.which(command)
        if path:
            return McpProbe(status="ok", detail=f"on PATH ({path})")
        return McpProbe(status="missing", detail=f"{command!r} is not on PATH")

    url = (row.get("url") or "").strip()
    if not url:
        return McpProbe(status="error", detail="http server has no url")
    if not _is_loopback(url):
        return McpProbe(
            status="remote",
            detail="not probed (non-loopback URL; HP-58 owns allowlisted fetch)",
        )
    try:
        with urlopen(url, timeout=timeout) as resp:  # noqa: S310 — loopback only
            code = getattr(resp, "status", None) or 200
        return McpProbe(status="ok", detail=f"loopback answered HTTP {code}")
    except Exception as exc:  # noqa: BLE001 — probe must never raise
        return McpProbe(status="error", detail=str(exc))


def probe_and_store(server_id: int, *, timeout: float = 1.5) -> dict[str, Any] | None:
    row = state_service.get_mcp_server(server_id)
    if row is None:
        return None
    result = probe_server(row, timeout=timeout)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    state_service.update_mcp_probe(
        server_id, status=result.status, detail=result.detail, probed_at=now
    )
    updated = state_service.get_mcp_server(server_id)
    return updated


def refresh_stale(max_age_seconds: int = 60) -> list[dict[str, Any]]:
    """Re-probe every server whose last probe is missing or older than
    ``max_age_seconds``. Used by GET /v1/mcp/servers so the page stays
    current without a dedicated scheduler."""
    now = datetime.now(timezone.utc)
    refreshed: list[dict[str, Any]] = []
    for row in state_service.list_mcp_servers():
        stamped = row.get("last_probe_at")
        stale = True
        if stamped:
            try:
                probed = datetime.strptime(str(stamped)[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                stale = (now - probed).total_seconds() > max_age_seconds
            except ValueError:
                stale = True
        if stale:
            updated = probe_and_store(int(row["id"]))
            if updated:
                refreshed.append(updated)
        else:
            refreshed.append(row)
    return refreshed
