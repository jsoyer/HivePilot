"""Agent auth: probe it, surface it, start a headless login — never touch a secret.

The pattern was proven three times on the box before it became code: per-user
auth stores under the SERVICE home, headless login flows that print a URL for
the operator to validate elsewhere, token born on the box so the "is this
token portable?" question never has to be answered. grok
(`grok login --device-auth` -> accounts.x.ai URL), cursor
(`NO_OPEN_BROWSER=1 cursor-agent login` -> cursor.com URL), gh (hosts.yml —
plus the lesson that root's gh LOOKED wired but was logged out: everything
auth lives under the service HOME, and a probe under any other HOME reads the
wrong world. This module runs IN the service, so its `~` is the right one by
construction).

Three rules:

  * VERIFIED-ONLY contracts. A kind nobody verified is absent from the
    registry and surfaces as "unknown" — the honoured_controls doctrine
    applied to auth. An invented store path would report "absent" for an
    agent that is in fact logged in, which is worse than admitting ignorance.
  * NO SECRET is ever read. The store probe checks existence and size, never
    content. The login path returns a URL and records THAT a login started —
    never a token, and never the log's other lines (some CLIs echo token
    material on success).
  * NO MODEL is ever invoked. Probes are file stats; the one live probe
    declared (gh) is its own auth-status command. `probe_agent_cli` taught
    what probes that spawn real work do to a process.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404 - only verified registry constants are ever launched
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "AUTH_CONTRACTS",
    "AgentAuthError",
    "AuthContract",
    "auth_state",
    "start_headless_login",
]


class AgentAuthError(RuntimeError):
    """A refused auth action. Operator-facing message."""


@dataclass(frozen=True)
class AuthContract:
    """What is VERIFIED about one agent's auth surface. All fields nullable —
    None always means "not verified", never "not needed"."""

    #: Path (``~`` = the service home) whose non-empty existence means
    #: "credentials stored". Existence, not validity: an expired token still
    #: reads "present", and claiming more would be a lie.
    auth_store: str | None = None
    #: The exact headless login argv, verified against the installed binary.
    login_argv: list[str] | None = None
    #: Environment the login needs (e.g. cursor's NO_OPEN_BROWSER).
    login_env: dict[str, str] = field(default_factory=dict)
    #: A cheap, model-free liveness probe (exit 0 = authenticated), where one
    #: exists. gh's own `auth status` is the only verified example.
    probe_argv: list[str] | None = None


#: kind -> verified contract. Everything here was PROVEN on the box
#: (2026-08-21/22); a kind not listed surfaces as "unknown".
AUTH_CONTRACTS: dict[str, AuthContract] = {
    "grok": AuthContract(
        auth_store="~/.grok/auth.json",
        # Prints https://accounts.x.ai/... and polls; token lands in the store.
        login_argv=["grok", "login", "--device-auth"],
    ),
    "cursor": AuthContract(
        auth_store="~/.config/cursor/auth.json",
        # "Set NO_OPEN_BROWSER to disable browser opening" — it then prints
        # the cursor.com/loginDeepControl URL instead.
        login_argv=["cursor-agent", "login"],
        login_env={"NO_OPEN_BROWSER": "1"},
    ),
    "claude": AuthContract(
        # Verified present on the box. A HEADLESS login command was NOT
        # verified, so none is declared — the UI gets no button, not a guess.
        auth_store="~/.claude/.credentials.json",
    ),
    "gh": AuthContract(
        auth_store="~/.config/gh/hosts.yml",
        probe_argv=["gh", "auth", "status"],
        # `gh auth login` wants an interactive confirmation; not verified
        # headless, so not declared.
    ),
}

#: How long the login scraper waits for the flow to print its URL.
_URL_WAIT_SECONDS = 15.0


def auth_state(kind: str) -> str:
    """``present`` | ``absent`` | ``unknown`` — and it never guesses.

    "present" means credentials are STORED (a non-empty store file), not that
    they are valid: an expired token still reads present, and claiming
    validity from a file stat would be a lie. Existence and size only — the
    content is never read.
    """
    contract = AUTH_CONTRACTS.get(kind)
    if contract is None or contract.auth_store is None:
        return "unknown"
    store = Path(os.path.expanduser(contract.auth_store))
    try:
        return "present" if store.stat().st_size > 0 else "absent"
    except OSError:
        return "absent"


def _login_log_dir() -> Path:
    """0700 under the service home: the log can echo flow details, so it gets
    the same protection as the stores themselves."""
    path = Path(os.path.expanduser("~/.hivepilot-logins"))
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _default_spawn(argv: list[str], *, env: dict[str, str], log_path: Path) -> None:
    """Launch the login DETACHED, its output to *log_path*. The process must
    outlive this request: the operator validates the URL on their own time,
    and the flow keeps polling until they do — the grok/cursor pattern."""
    with log_path.open("wb") as log:
        subprocess.Popen(  # noqa: S603 - verified registry constants only
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **env},
            start_new_session=True,
        )


def start_headless_login(
    kind: str,
    *,
    spawn: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Start *kind*'s verified headless login and return the URL to open.

    Refuses (never improvises) for a kind with no verified login command.
    Returns ``{"url": <first https line> | None, "log": <path>}`` — the URL
    ONLY, never the rest of the log: some CLIs echo token material on
    success, and a response is one serialisation away from a leak. ``None``
    means the flow printed nothing URL-shaped in the window; the log path is
    returned so an operator can read it ON THE BOX.
    """
    contract = AUTH_CONTRACTS.get(kind)
    if contract is None or contract.login_argv is None:
        raise AgentAuthError(
            f"{kind!r} has no verified headless login flow. Verified flows "
            f"exist for: "
            f"{', '.join(sorted(k for k, c in AUTH_CONTRACTS.items() if c.login_argv))}. "
            f"None is not a guessable gap — verify the flow first."
        )

    log_path = _login_log_dir() / f"{kind}-login.log"
    launcher = spawn or _default_spawn
    logger.info("agent_auth.login_started", kind=kind)
    launcher(list(contract.login_argv), env=dict(contract.login_env), log_path=log_path)

    deadline = time.monotonic() + _URL_WAIT_SECONDS
    url: str | None = None
    while time.monotonic() < deadline:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        match = re.search(r"https://\S+", text)
        if match:
            url = match.group(0)
            break
        time.sleep(0.2)

    return {"kind": kind, "url": url, "log": str(log_path)}
