"""Loopback Chrome DevTools tabs (HP-68 slice 2).

Real tabs only — HivePilot does not invent a browser. A loopback CDP
endpoint (Chrome ``--remote-debugging-port``) is listed when it answers.
Non-loopback URLs are refused (same SSRF contract as local-model discovery).
Debugger WebSocket URLs are never returned.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from hivepilot.services.local_models import is_loopback_url

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
_TIMEOUT = 1.2

_NOTE_ATTACHED = (
    "Tabs from a loopback Chrome DevTools endpoint. HivePilot does not own this browser."
)
_NOTE_EMPTY = (
    "No loopback browser is attached. Start Chrome with "
    "--remote-debugging-port=9222 or set HIVEPILOT_BROWSER_CDP_URL."
)
_NOTE_REFUSED = "Discovery only probes loopback — remote CDP is refused."


def default_cdp_url() -> str:
    return os.environ.get("HIVEPILOT_BROWSER_CDP_URL") or DEFAULT_CDP_URL


def snapshot(*, base_url: str | None = None, timeout: float = _TIMEOUT) -> dict[str, Any]:
    url = (base_url or default_cdp_url()).strip() or DEFAULT_CDP_URL
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        url = DEFAULT_CDP_URL
    if not is_loopback_url(url):
        return {
            "attached": False,
            "base_url": url,
            "tabs": [],
            "note": _NOTE_REFUSED,
            "error": "refused: discovery only probes loopback",
        }
    list_url = url.rstrip("/") + "/json/list"
    try:
        with urllib.request.urlopen(list_url, timeout=timeout) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {
            "attached": False,
            "base_url": url,
            "tabs": [],
            "note": _NOTE_EMPTY,
            "error": f"HTTP {exc.code}",
        }
    except (OSError, ValueError) as exc:
        return {
            "attached": False,
            "base_url": url,
            "tabs": [],
            "note": _NOTE_EMPTY,
            "error": str(exc) or "unreachable",
        }
    tabs = _normalize_tabs(raw)
    return {
        "attached": True,
        "base_url": url,
        "tabs": tabs,
        "note": _NOTE_ATTACHED if tabs else _NOTE_EMPTY,
        "error": None,
    }


def _normalize_tabs(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "page")
        if kind not in {"page", "webview"}:
            continue
        title = str(item.get("title") or "")
        href = str(item.get("url") or "")
        ident = str(item.get("id") or href or title)
        if not ident:
            continue
        out.append({"id": ident, "title": title, "url": href, "type": kind})
    return out
