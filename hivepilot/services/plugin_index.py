"""Phase 26b Approach A — plugin discovery via a metadata INDEX.

Fetches and parses a small JSON document (the "plugin index") describing
plugins available for the operator to install: name, description, author,
homepage, an install hint (pip package name or git URL), version, and
checksum.

CRITICAL trust-model invariant (see docs/v4/PLUGINS.md "Trust model" —
"There is no network fetch of plugin CODE, ever."): this module fetches
METADATA ONLY. It never downloads, imports, or executes plugin code.
Installation stays on the operator's own `pip install` / `git clone` —
`plugins info` only ever *prints* the command for the operator to run
themselves; nothing in this module (or the CLI commands built on top of it)
triggers an install, subprocess, or import of anything fetched over the
network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PluginIndexEntry:
    """One plugin's metadata as advertised by the index. Display-only —
    never used to import or execute anything."""

    name: str
    description: str
    author: str | None = None
    homepage: str | None = None
    # e.g. {"type": "pip", "target": "hivepilot-plugin-foo"} or
    # {"type": "git", "target": "https://github.com/org/repo"}
    install: dict[str, str] = field(default_factory=dict)
    version: str | None = None
    checksum: str | None = None
    contributes: list[str] | None = None  # runner/notifier/etc. names — display only


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _parse_entry(raw: dict[str, Any]) -> PluginIndexEntry | None:
    """Parse a single raw index entry. Returns None (caller logs + skips)
    when the entry doesn't have the minimum required shape, so one bad
    entry never fails the whole index fetch."""
    name = raw.get("name")
    description = raw.get("description")
    if not isinstance(name, str) or not name or not isinstance(description, str):
        return None

    install = raw.get("install")
    if not isinstance(install, dict):
        install = {}
    else:
        install = {str(k): str(v) for k, v in install.items()}

    contributes = raw.get("contributes")
    if not isinstance(contributes, list) or not all(isinstance(c, str) for c in contributes):
        contributes = None

    return PluginIndexEntry(
        name=name,
        description=description,
        author=_optional_str(raw, "author"),
        homepage=_optional_str(raw, "homepage"),
        install=install,
        version=_optional_str(raw, "version"),
        checksum=_optional_str(raw, "checksum"),
        contributes=contributes,
    )


def fetch_index(url: str | None = None, *, timeout: int = 10) -> list[PluginIndexEntry]:
    """Fetch and parse the plugin metadata index.

    METADATA ONLY. Performs a single GET of a JSON document describing
    available plugins; installation stays on the operator's own trusted
    pip/git path (see module docstring + docs/v4/PLUGINS.md "Trust model").

    Fail-safe: raises `RuntimeError` with a short, friendly reason — never
    the raw response body/exception text (which could leak internal
    hostnames/stack traces) — on: no index URL configured, network error,
    timeout, non-200 status, or invalid JSON. Individual malformed entries
    within an otherwise-valid index are skipped (logged) rather than
    failing the whole fetch.
    """
    resolved_url = url or settings.plugins_index_url
    if not resolved_url:
        raise RuntimeError("no plugin index configured — set HIVEPILOT_PLUGINS_INDEX_URL")

    try:
        response = requests.get(resolved_url, timeout=timeout)
    except requests.Timeout as exc:
        raise RuntimeError("plugin index request timed out") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"failed to reach plugin index ({type(exc).__name__})") from exc

    if response.status_code != 200:
        raise RuntimeError(f"plugin index returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("plugin index response was not valid JSON") from exc

    raw_entries = payload.get("plugins") if isinstance(payload, dict) else payload
    if not isinstance(raw_entries, list):
        raise RuntimeError("plugin index JSON did not contain a plugin list")

    entries: list[PluginIndexEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            logger.warning("plugin_index.malformed_entry", raw_type=type(raw).__name__)
            continue
        entry = _parse_entry(raw)
        if entry is None:
            logger.warning("plugin_index.malformed_entry", name=raw.get("name"))
            continue
        entries.append(entry)
    return entries


def search_index(entries: list[PluginIndexEntry], query: str) -> list[PluginIndexEntry]:
    """Case-insensitive substring match on name+description. Empty query
    returns every entry."""
    if not query:
        return list(entries)
    needle = query.lower()
    return [
        entry
        for entry in entries
        if needle in entry.name.lower() or needle in entry.description.lower()
    ]


def format_install_hint(install: dict[str, str]) -> str:
    """Render the operator-facing install command for a `PluginIndexEntry.install`
    dict. Display-only string — never executed by HivePilot itself."""
    kind = install.get("type")
    target = install.get("target")
    if not target:
        return "-"
    if kind == "pip":
        return f"pip install {target}"
    if kind == "git":
        return f"git clone {target}"
    return target
