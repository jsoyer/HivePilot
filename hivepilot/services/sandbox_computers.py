"""Sandbox computers spike — HP-67 decision + HP-72 fail-closed take-over.

HP-67 decision (2026-09-10): **no-go** on shipping a Docker / E2B / Daytona /
Box provider in this spike.

HivePilot already confines *CLI runners* via ``hivepilot.utils.sandbox``
(bwrap + env scrub). That is not a per-agent desktop, not a per-agent
network, and not a signed screen-proxy. Standing up a ``SandboxProvider``
now would invent a security boundary we cannot enforce:

- token-gated supervisor start/stop
- network isolation per agent
- signed screen-proxy (never the host X11/Wayland session)

A half-built provider that reports fake sandboxes would be a lie. The only
honest computer surface shipped here is HP-68 (this-host RAM/CPU/disk,
allowlisted processes, loopback CDP tabs).

HP-72: take-over / hand-back / skip require an attached sandbox desktop.
None is attached. Every control action is refused. HivePilot does not bind
those actions to the host machine.
"""

from __future__ import annotations

from typing import Any, Literal

EVALUATED_PROVIDERS = ("docker", "e2b", "daytona", "box")
DECISION: Literal["no-go"] = "no-go"

_PROVIDER_NOTE = (
    "No sandbox-computer provider is configured. Docker/E2B/Daytona/Box "
    "were evaluated; this spike is a no-go. CLI bwrap is not a desktop."
)
_SESSION_NOTE = (
    "No sandbox desktop is attached. Take-over is refused rather than bound to this host."
)
_REFUSED = "refused: no sandbox desktop is attached"


def provider_snapshot() -> dict[str, Any]:
    return {
        "configured": False,
        "provider": None,
        "decision": DECISION,
        "evaluated": list(EVALUATED_PROVIDERS),
        "note": _PROVIDER_NOTE,
    }


def session_snapshot() -> dict[str, Any]:
    return {
        "attached": False,
        "can_takeover": False,
        "controller": None,
        "note": _SESSION_NOTE,
    }


def control(action: Literal["takeover", "handback", "skip"]) -> dict[str, Any]:
    """Fail-closed human control. Never invents a session."""
    return {
        "ok": False,
        "action": action,
        "attached": False,
        "error": _REFUSED,
        "note": _SESSION_NOTE,
    }
