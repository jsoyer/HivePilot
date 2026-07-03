"""Worker health registry (W2) — pull model.

The hub discovers worker URLs from role hosts (any http(s):// host is a worker),
pings each worker's ``/health``, and records the result in state_service so
``hivepilot workers`` can show who is up. No worker-side heartbeat loop needed.
"""

from __future__ import annotations

import requests

from hivepilot.roles import ROLES, resolve_host
from hivepilot.services import state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def ping_worker(url: str, timeout: int = 5) -> tuple[bool, str | None]:
    """Return (live, detail) by GETting the worker's /health endpoint."""
    try:
        resp = requests.get(url.rstrip("/") + "/health", timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if resp.status_code == 200:
        return True, None
    return False, f"status {resp.status_code}"


def _worker_urls() -> set[str]:
    """Worker URLs referenced by role hosts (http(s):// = worker; ssh alias = not)."""
    urls: set[str] = set()
    for name in ROLES:
        host = resolve_host(name)
        if host and host.startswith(("http://", "https://")):
            urls.add(host)
    return urls


def refresh() -> list[dict]:
    """Ping every known worker, persist its health, and return the worker list."""
    for url in sorted(_worker_urls()):
        live, detail = ping_worker(url)
        state_service.upsert_worker(url, url, "live" if live else "unreachable", detail)
        logger.info("worker.health", url=url, live=live)
    return state_service.list_workers()
