"""One way to talk to a live agent, with herdr and Orca as two drivers.

The operator's ask — see the agents work, and talk to them — is a shape, not a
missing feature. Roles already carry a name, a model, a memory and skills, and
they already message each other. What was missing is a single place that says
*start / inject / read / wait*, so herdr and Orca are two backends rather than
two parallel implementations.

Both expose the same four verbs, read from the installed binaries on
2026-08-18::

    herdr  agent prompt <target> <text>
           agent read <target> --lines <n>
           agent wait <target> --until <state> --timeout <ms>
    orca   terminal send --terminal <h> --text <t> --enter
           terminal read --terminal <h> --limit <n>
           terminal wait --terminal <h> --for tui-idle --timeout-ms <ms>

**The state vocabulary is herdr's, deliberately.** herdr distinguishes
`blocked` from `idle`; Orca's `tui-idle` does not. A false idle would cut a
step while the agent is still thinking — open point 5 of the ORCA-1 PRD.
Mapping the poorer vocabulary onto the richer one keeps the distinction
available where a backend can express it, and makes the driver REFUSE where it
cannot, rather than quietly degrading `blocked` into `idle`.

This module builds argv and nothing else. Execution belongs to the caller, so
the part that can be wrong silently — a flag name, a missing `--enter`, an
unbounded read — is testable without either binary installed.
"""

from __future__ import annotations

from enum import Enum

#: On Linux outside an Orca-managed terminal, bare `orca` resolves to the GNOME
#: screen reader and running it starts speech on the operator's machine. The
#: shim is the only safe entry point here.
ORCA_BIN = "orca-ide"
HERDR_BIN = "herdr"


class AgentState(str, Enum):
    """herdr's vocabulary: the richer of the two, so nothing is lost."""

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"


class HerdrDriver:
    """herdr: a persistent server holding real terminals, driven over its
    socket API. Targets are agent names."""

    name = "herdr"

    def send_argv(self, target: str, text: str) -> list[str]:
        return [HERDR_BIN, "agent", "prompt", target, text]

    def read_argv(self, target: str, *, limit: int) -> list[str]:
        # Bounded on purpose: an unbounded read of a live pane is how a
        # 26 000-character report ends up back inside a prompt.
        return [HERDR_BIN, "agent", "read", target, "--lines", str(limit)]

    def wait_argv(self, target: str, *, until: AgentState, timeout_ms: int) -> list[str]:
        # `herdr agent wait` without --timeout waits INDEFINITELY, per its own
        # help. A step that hangs forever is worse than one that fails.
        return [
            HERDR_BIN,
            "agent",
            "wait",
            target,
            "--until",
            until.value,
            "--timeout",
            str(timeout_ms),
        ]


class OrcaDriver:
    """Orca: an ADE whose terminals are addressed by handle."""

    name = "orca"

    def send_argv(self, target: str, text: str) -> list[str]:
        # `--enter` is not optional in practice: without it the text sits on
        # the prompt line unsent, the agent reads as idle, and the operator
        # waits for an answer that was never asked for.
        return [ORCA_BIN, "terminal", "send", "--terminal", target, "--text", text, "--enter"]

    def read_argv(self, target: str, *, limit: int) -> list[str]:
        return [ORCA_BIN, "terminal", "read", "--terminal", target, "--limit", str(limit)]

    def wait_argv(self, target: str, *, until: AgentState, timeout_ms: int) -> list[str]:
        if until is not AgentState.IDLE:
            # Orca exposes only `tui-idle`. Silently accepting BLOCKED and
            # waiting for idle instead would reintroduce the false-idle cut
            # this vocabulary exists to prevent, so refuse and name it.
            raise ValueError(
                f"Orca can only wait for {AgentState.IDLE.value!r} (`--for tui-idle`); "
                f"it cannot express {until.value!r}. Use the herdr driver where that "
                f"distinction matters."
            )
        return [
            ORCA_BIN,
            "terminal",
            "wait",
            "--terminal",
            target,
            "--for",
            "tui-idle",
            "--timeout-ms",
            str(timeout_ms),
        ]


_DRIVERS: dict[str, type[HerdrDriver] | type[OrcaDriver]] = {
    "herdr": HerdrDriver,
    "orca": OrcaDriver,
}


def resolve_driver(backend: str) -> HerdrDriver | OrcaDriver:
    """Return the driver for *backend*, or raise naming the rejected value.

    "Unsupported backend" sends an operator nowhere; they set this in config
    and need to see which value was refused.
    """
    cls = _DRIVERS.get((backend or "").strip().lower())
    if cls is None:
        raise ValueError(
            f"unknown agent surface backend {backend!r}; known: {', '.join(sorted(_DRIVERS))}"
        )
    return cls()
