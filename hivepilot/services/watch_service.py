"""Follow the event stream — the process a herdr pane runs.

herdr has no PTY-less pane: a `pane` node without `command` starts a shell
rather than becoming a passive surface, and popups carry no `pane_id` so
they are not addressable. A pane must therefore run *something*, and the
board is built by giving it the cheapest stable thing there is — a follower.

That choice is not a workaround. A pane running the agent itself would
blink out when the step ended, and restoring a saved layout would
**re-execute the agents**. A follower outlives any run, so the board stands
between pipelines and `layout.apply` merely re-attaches it.

Two hazards shape this module.

**The log path is a cwd-relative silo**, exactly like `state.db`: `logs_dir`
defaults to `runs/logs` relative to the working directory. The production
box carries five `hivepilot.log` files, one per directory the CLI was ever
run from, two empty since July; only `/runs/logs/` is live, because the
units set `WorkingDirectory=/`. Opening the wrong one displays nothing,
which is indistinguishable from a quiet system — so this module never just
opens a file, it *describes* the file it opened first.

**The stream is untrusted text bound for a terminal.** Agent output is
quoted verbatim into `detail`. Every rendered field goes through
`strip_control_chars`, which removes escape sequences and also stops an
embedded newline from fabricating a line that looks like a real event.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hivepilot.utils.terminal import strip_control_chars

__all__ = [
    "LogSource",
    "describe_source",
    "event_matches",
    "follow_lines",
    "parse_event",
    "render_event",
    "resolve_log_source",
]

_LOG_FILENAME = "hivepilot.log"

#: Seconds between reads while following. Small enough to feel live, large
#: enough that an idle board is not a busy loop on the operator's machine.
_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class LogSource:
    path: Path
    exists: bool
    size: int
    mtime: datetime | None


def resolve_log_source(*, logs_dir: str | Path) -> LogSource:
    """Locate the event log and report what is actually there.

    Absolute by construction: a relative path in the description would not
    tell an operator *which* of the candidate files was opened, which is the
    entire failure this guards against.
    """
    path = (Path(logs_dir).expanduser().resolve()) / _LOG_FILENAME
    if not path.is_file():
        return LogSource(path=path, exists=False, size=0, mtime=None)
    stat = path.stat()
    return LogSource(
        path=path,
        exists=True,
        size=stat.st_size,
        mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def describe_source(source: LogSource) -> str:
    """One line, printed before any event, naming what is being followed.

    Silence has two very different causes — nothing is happening, or nothing
    was ever written here — and on screen they look the same. This is what
    tells them apart.
    """
    if not source.exists:
        return f"watching {source.path} — does not exist (nothing will appear)"
    if source.size == 0:
        return f"watching {source.path} — empty, never written (nothing will appear)"
    when = source.mtime.strftime("%Y-%m-%d %H:%M:%S UTC") if source.mtime else "unknown"
    return f"watching {source.path} — {source.size} bytes, last written {when}"


def parse_event(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line, or return None.

    Tolerant on purpose: the log is shared with anything else writing to
    stderr, and a follower that dies on a malformed line takes the board
    down with it.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def event_matches(
    event: dict[str, Any],
    *,
    roles: set[str] | None = None,
    run_id: int | None = None,
) -> bool:
    """Whether *event* belongs on this pane.

    A role filter drops events with no role. That is deliberate: a step no
    model performed carries `role: null` by design, and showing it on a pane
    dedicated to one agent would undo exactly the attribution honesty the
    event was given.
    """
    if roles is not None:
        role = event.get("role")
        if not isinstance(role, str) or role not in roles:
            return False
    if run_id is not None and event.get("run_id") != run_id:
        return False
    return True


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return strip_control_chars(value, replacement=" ")
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def render_event(event: dict[str, Any], *, as_json: bool = False) -> str:
    """Render one event as a single terminal-safe line.

    `as_json` exists for piping, which does not make the bytes safe — the
    other end of a pipe is usually another terminal — so it is stripped
    exactly the same way.
    """
    cleaned = {k: _clean(v) for k, v in event.items()}
    if as_json:
        return json.dumps(cleaned)

    stamp = str(cleaned.get("timestamp", ""))[11:19] or "--:--:--"
    parts = [
        stamp,
        f"run {cleaned['run_id']}" if cleaned.get("run_id") is not None else "run ?",
        str(cleaned.get("role") or "-"),
        str(cleaned.get("step") or cleaned.get("event") or "-"),
        str(cleaned.get("status") or ""),
    ]
    detail = cleaned.get("detail")
    if isinstance(detail, str) and detail.strip():
        parts.append(f"— {detail.strip()}")
    return "  ".join(p for p in parts if p)


def follow_lines(
    path: Path, *, from_start: bool = False, poll_seconds: float = _POLL_SECONDS
) -> Iterator[str]:
    """Yield lines as they are appended, surviving rotation and late creation.

    Reopens when the inode changes or the file shrinks, so a rotated or
    recreated log resumes rather than hanging forever on a deleted handle —
    a board that silently stops after logrotate is the same failure as
    opening the wrong file.
    """
    handle = None
    inode = None
    opened_before = False
    try:
        while True:
            if handle is None:
                if not path.is_file():
                    time.sleep(poll_seconds)
                    continue
                handle = path.open("r", encoding="utf-8", errors="replace")
                inode = path.stat().st_ino
                # Skip history only on the FIRST open. After a rotation or a
                # truncation this is a file we have never read, so seeking to
                # its end would silently discard everything written between
                # the rotation and the reopen -- and the board would look
                # merely idle rather than broken.
                if not from_start and not opened_before:
                    handle.seek(0, 2)
                opened_before = True

            line = handle.readline()
            if line:
                yield line
                continue

            time.sleep(poll_seconds)
            try:
                current = path.stat()
            except OSError:
                handle.close()
                handle = None
                continue
            if current.st_ino != inode or current.st_size < handle.tell():
                handle.close()
                handle = None
    finally:
        if handle is not None:
            handle.close()
