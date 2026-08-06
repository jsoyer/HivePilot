"""Build the herdr layout that turns a set of roles into a standing board.

One pane per role, each running `hivepilot watch --role <name>`.

The rule everything here obeys comes from herdr's own semantics: a `pane`
node may legally omit `command`, but `layout.apply` then launches the
default shell. **Absent `command` means "start a shell", not "a pane with
nothing in it"** -- there is no public API for a purely visual or stable
PTY-less pane, and popups are not addressable (`pane.*` needs a `pane_id`
they do not have). A layout generated without commands applies cleanly and
produces a wall of idle prompts: a board that looks like it works and
reports nothing.

Nothing here invents an identifier. herdr's workspace/tab/pane ids are
opaque and are parsed from its output; a layout that hand-builds `w1:p1` is
guessing at another process's internal state.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

__all__ = ["build_board_layout"]

#: Role names land inside a shell command. Anything outside a plain
#: identifier is refused rather than escaped: roles come from config, so an
#: exotic one is a mistake worth surfacing, not something to accommodate.
_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def build_board_layout(
    roles: list[str],
    *,
    remote: str | None = None,
    log_path: str | None = None,
) -> dict[str, Any]:
    """Return a herdr layout: one follower pane per role.

    ``remote`` pipes the stream over ssh instead of reading a local file --
    the board runs where the operator is, the services run on the box, and
    `--stdin` keeps that working without anything new listening anywhere.

    ``log_path`` is required with ``remote`` and is deliberately not guessed:
    ``logs_dir`` is cwd-relative, the production box carries five candidate
    `hivepilot.log` files and four are stale. A wrong guess yields a board
    that is silent and plausible, which is the worst of both.
    """
    seen: list[str] = []
    for role in roles:
        if not _ROLE_RE.match(role):
            raise ValueError(
                f"role {role!r} is not a plain identifier; it would land inside a shell command"
            )
        if role not in seen:
            seen.append(role)
    if not seen:
        raise ValueError("a board needs at least one role")

    if remote is not None and not log_path:
        raise ValueError("log_path is required with remote: the log path must not be guessed")

    return {
        "type": "workspace",
        "label": "hivepilot",
        "children": [
            {
                "type": "pane",
                "label": role,
                "command": _pane_command(role, remote=remote, log_path=log_path),
            }
            for role in seen
        ],
    }


def _pane_command(role: str, *, remote: str | None, log_path: str | None) -> str:
    if remote is None:
        return f"hivepilot watch --role {role}"
    inner = f"tail -F {shlex.quote(log_path or '')}"
    return f"ssh {shlex.quote(remote)} {shlex.quote(inner)} | hivepilot watch --stdin --role {role}"
