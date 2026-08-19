"""Two probes for the things that fail by going quiet.

Both answer a question nothing else on the dashboard can, because both fail the
same way: they stop producing, and an absence looks exactly like a healthy
zero.

    the agent surface -- is a backend configured at all, and if one is, does it
    answer? A view that renders every role as idle because it cannot reach the
    server is worse than one that admits it cannot reach it;

    OTel export -- is telemetry still ARRIVING? An exporter that quietly
    stopped leaves a row count that still looks fine and a newest row from last
    Tuesday. That is the plausible zero somebody spends a day chasing.

Three states each, and the middle one carries the value:

    not configured   -- nothing is wrong; nobody asked for this
    configured, dead -- something IS wrong, and it is actionable
    working          -- with the evidence that says so

Folding the first two together into "unhealthy" cries wolf on every deployment
that simply does not use the feature. Folding them into "ok" hides the outage.

Pure-ish: the readings are passed in, so the rules below can be argued with
without a database or a subprocess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

#: The only backends this knows how to probe. A configured value outside this
#: set is reported as such rather than being handed to a shell -- a typo in
#: configuration must never become a command.
_PROBE_ARGV: dict[str, list[str]] = {
    "herdr": ["herdr", "status"],
    # `orca-ide`, NEVER a bare `orca`: outside Orca's own terminals that name
    # resolves to the GNOME Orca screen reader and starts speech on the
    # operator's machine.
    "orca": ["orca-ide", "status", "--json"],
}

#: Floor for the staleness window. A zero or negative threshold would mark
#: every row stale or every row fresh depending on which way the comparison
#: falls -- either way the probe stops measuring anything.
_MIN_STALE_HOURS = 1


def probe_agent_surface(
    *,
    backend: str,
    run_cli: Callable[[list[str]], int],
) -> dict[str, Any]:
    """Whether the configured live-agent surface answers.

    `backend` is `settings.agent_surface_backend`: `herdr`, `orca`, or empty.
    Empty is the default and the honest one -- with no backend there is nothing
    to see, and that is not a fault.
    """
    name = (backend or "").strip().lower()
    if not name:
        return {"state": "not_configured", "backend": None}

    argv = _PROBE_ARGV.get(name)
    if argv is None:
        return {"state": "unknown_backend", "backend": name}

    try:
        code = run_cli(argv)
    except Exception as exc:  # noqa: BLE001
        # A missing binary RAISES rather than returning non-zero, and reading
        # that as healthy is exactly how a dead surface looks alive.
        logger.warning("probe.agent_surface_failed", backend=name, error=str(exc))
        return {"state": "unreachable", "backend": name}

    return {"state": "ok" if code == 0 else "unreachable", "backend": name}


def probe_otel_arrival(
    *,
    rows: int,
    newest: str | None,
    stale_after_hours: int = 6,
) -> dict[str, Any]:
    """Whether agent telemetry is still arriving, not merely present.

    The count is the misleading half: it stays healthy forever once an
    exporter has ever worked. Only the AGE of the newest row says whether it
    still does.

    `never_arrived` and `stale` are deliberately different answers. The first
    points at configuration -- nothing has ever come. The second points at an
    exporter that used to work and stopped, which is a different investigation
    entirely.
    """
    if rows <= 0:
        return {"state": "never_arrived", "rows": 0, "age_hours": None}

    parsed = _parse_timestamp(newest)
    if parsed is None:
        # A row we cannot date cannot attest freshness. Reporting it as fresh
        # is the failure this probe exists to catch, so it says "unknown".
        return {"state": "unknown", "rows": rows, "age_hours": None}

    age = datetime.now(timezone.utc) - parsed
    window = timedelta(hours=max(_MIN_STALE_HOURS, stale_after_hours))
    return {
        "state": "stale" if age > window else "ok",
        "rows": rows,
        "age_hours": round(age.total_seconds() / 3600, 2),
    }


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Parse a stored timestamp as UTC, or None.

    Never raises and never guesses: an unparseable value makes the caller
    report `unknown`, which is the honest answer for a row whose age nobody
    can establish.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
