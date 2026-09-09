"""This-host allowlisted processes (HP-68 slice 2).

Reads ``/proc/<pid>/comm`` + ``VmRSS``. Only names HivePilot already treats
as agent/runtime binaries are returned — not a full ``ps`` dump, and never
the process command line (that can carry tokens).
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Callable

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_PROC = Path("/proc")
_MAX_ROWS = 24

#: Exact ``comm`` names (15-char kernel cap). Prefixes live in
#: ``_COMM_PREFIXES`` for ``cursor-agent`` / ``google-chrome``.
_COMM_EXACT = frozenset(
    {
        "hivepilot",
        "ocx",
        "ollama",
        "claude",
        "codex",
        "opencode",
        "pi",
        "qwen",
        "kimi",
        "grok",
        "chrome",
        "chromium",
    }
)
_COMM_PREFIXES = ("cursor-agent", "google-chrome", "chrome-headless")

_NOTE = (
    "Allowlisted agent/runtime processes on this HivePilot host. "
    "Not a full process table and not a fleet."
)
_UNAVAILABLE = "No readable /proc — process list is omitted, not invented."


def _matches(comm: str) -> bool:
    name = comm.strip()
    if name in _COMM_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _COMM_PREFIXES)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _rss_bytes(status_text: str) -> int | None:
    for line in status_text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) * 1024
    return None


def snapshot(
    *,
    proc_root: Path | None = None,
    hostname: str | None = None,
    read_text: Callable[[Path], str | None] = _read_text,
) -> dict[str, Any]:
    root = proc_root if proc_root is not None else _PROC
    host = hostname if hostname is not None else socket.gethostname()
    if not root.is_dir():
        return {
            "host": host,
            "processes": [],
            "note": _UNAVAILABLE,
        }
    rows: list[dict[str, Any]] = []
    try:
        pids = list(root.iterdir())
    except OSError as exc:
        logger.info("host_processes.list_failed", error=str(exc))
        return {"host": host, "processes": [], "note": _UNAVAILABLE}
    for entry in pids:
        if not entry.name.isdigit():
            continue
        comm = read_text(entry / "comm")
        if not comm or not _matches(comm):
            continue
        status = read_text(entry / "status")
        rows.append(
            {
                "pid": int(entry.name),
                "name": comm.strip(),
                "rss_bytes": _rss_bytes(status) if status else None,
            }
        )
    rows.sort(key=lambda r: (r["rss_bytes"] is None, -(r["rss_bytes"] or 0), r["pid"]))
    return {
        "host": host,
        "processes": rows[:_MAX_ROWS],
        "note": _NOTE,
    }
