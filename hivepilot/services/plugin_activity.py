"""Per-plugin *exercised* evidence — deliberately distinct from plugin health.

A plugin's own ``health()`` answers **"am I installed and configured?"**: the
import succeeded, the API key is present, the binary is on ``PATH``. That is a
useful question, but it is not the one an operator actually asks when a feature
looks dead, which is **"did this plugin do anything?"**.

The two answers can disagree for a long time without anything looking wrong.
The `headroom` and `mem0` plugins both reported ``status="ok"`` for weeks while
failing every single call — `headroom` was handed a ``str`` where
``compress()`` wants ``list[dict]``, and `mem0` was passing ``user_id=`` to a
``search()`` that wants ``filters=``. Health was telling the truth about
installation the whole time. Nothing was telling the truth about use.

This module supplies the second answer, and only where it can be *proved* from
telemetry a plugin demonstrably wrote itself. Plugins with no telemetry get
``None`` — the caller must then say "presence check only" rather than let a
green health badge imply the plugin is working.

**Attribution must be provable, never assumed.** A probe may only claim a table
when exactly one plugin writes to it:

- ``headroom_compressions`` / ``headroom_skips`` — written solely by
  `plugins/headroom.py` via `hivepilot.services.headroom_metrics`.
- ``memory_events`` — written by `plugins/mem0.py`, `plugins/obsidian.py`
  and `plugins/hindsight.py` via `hivepilot.services.memory_service`, and
  attributed by its ``backend`` column rather than by a call-site assumption.
  It used to rest on mem0 being the sole writer; a tripwire test guarded that
  and fired the moment Obsidian (then Hindsight) was instrumented, which is
  exactly what it was for. A NULL backend counts as mem0: every row predating
  the column was written by it.

**Tenant-scoped.** Both source tables are tenant-partitioned. ``tenant=None``
means unscoped (admin), matching the `_analytics_tenant` convention in
`api_service`. Plugin *health* is process-global, but plugin *activity* is not,
and mixing the two scopes would leak one tenant's timing to another.

**Read-only and non-raising by contract.** Probes never write (beyond the
idempotent ``init_db()`` of each table's owning module) and never propagate an
exception: ``GET /v1/plugins/health`` must not be able to 500 because a metrics
table is unreadable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import structlog

from hivepilot.services import db

logger = structlog.get_logger(__name__)

# How far back `events` counts. `last_used` is deliberately NOT bounded by this
# window -- a plugin last exercised 90 days ago should still be able to say
# *when*, rather than collapse to the same "never" as one that has never run.
DEFAULT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class PluginActivity:
    """Evidence that a plugin actually ran.

    ``last_used`` is the most recent event timestamp ever recorded (UTC, naive
    — the SQLite ``CURRENT_TIMESTAMP`` convention used throughout HivePilot),
    or ``None`` when the plugin has no recorded event at all.

    ``events`` counts events inside ``window_days`` only, so a busy-then-idle
    plugin reads as ``last_used`` far in the past with ``events=0``.

    ``evidence`` names the tables the numbers came from, so the UI can show the
    operator what was measured instead of asking them to trust a badge.
    """

    last_used: str | None
    events: int
    window_days: int
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _probe(
    *,
    tables: tuple[str, ...],
    evidence: str,
    tenant: str | None,
    window_days: int,
    backend: str | None = None,
) -> PluginActivity:
    """Aggregate ``MAX(ts)`` and a windowed count across *tables*.

    Every table in *tables* must expose a ``tenant`` and a ``ts`` column. The
    ``UNION ALL`` keeps a plugin whose record is split across several tables
    (headroom writes successes and declines separately) reading as one plugin
    rather than two half-answers.

    *backend* filters on a ``backend`` column when the table has one. It exists
    because ``memory_events`` now has two writers: without it, mem0's row would
    absorb Obsidian's recalls and read as busier than it is. A NULL backend
    counts as mem0 -- every row predating the column was written by it, which
    was the only instrumented backend.
    """
    conditions: list[str] = []
    params: tuple[Any, ...] = ()
    if tenant is not None:
        conditions.append("tenant = ?")
        params += (tenant,)
    if backend is not None:
        conditions.append("COALESCE(backend, 'mem0') = ?")
        params += (backend,)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    union = " UNION ALL ".join(f"SELECT ts FROM {table}{where}" for table in tables)
    # Placeholders repeat once per UNION branch.
    all_params = params * len(tables)

    with db.connect() as conn:
        row = conn.execute(
            db.ph(f"SELECT MAX(ts) AS last_used FROM ({union})"),
            all_params,
        ).fetchone()
        # `datetime('now', '-N days')` keeps the cutoff in the database's own
        # clock and format. Comparing against a Python-built string would stake
        # the window on this process's timezone matching CURRENT_TIMESTAMP's
        # UTC -- an assumption that silently shifts the boundary when it fails.
        counted = conn.execute(
            db.ph(
                f"SELECT COUNT(*) AS cnt FROM ({union}) "
                f"WHERE ts >= datetime('now', '-{int(window_days)} days')"
            ),
            all_params,
        ).fetchone()

    last_used = row["last_used"] if row is not None else None
    return PluginActivity(
        last_used=str(last_used) if last_used else None,
        events=int(counted["cnt"] or 0) if counted is not None else 0,
        window_days=window_days,
        evidence=evidence,
    )


def _headroom_activity(*, tenant: str | None, window_days: int) -> PluginActivity:
    from hivepilot.services import headroom_metrics

    headroom_metrics.init_db()
    return _probe(
        tables=("headroom_compressions", "headroom_skips"),
        evidence="headroom_compressions + headroom_skips",
        tenant=tenant,
        window_days=window_days,
    )


def _mem0_activity(*, tenant: str | None, window_days: int) -> PluginActivity:
    from hivepilot.services import memory_service

    memory_service.init_db()
    return _probe(
        tables=("memory_events",),
        evidence="memory_events (backend=mem0)",
        tenant=tenant,
        window_days=window_days,
        backend="mem0",
    )


def _obsidian_activity(*, tenant: str | None, window_days: int) -> PluginActivity:
    from hivepilot.services import memory_service

    memory_service.init_db()
    return _probe(
        tables=("memory_events",),
        evidence="memory_events (backend=obsidian)",
        tenant=tenant,
        window_days=window_days,
        backend="obsidian",
    )


def _hindsight_activity(*, tenant: str | None, window_days: int) -> PluginActivity:
    from hivepilot.services import memory_service

    memory_service.init_db()
    return _probe(
        tables=("memory_events",),
        evidence="memory_events (backend=hindsight)",
        tenant=tenant,
        window_days=window_days,
        backend="hindsight",
    )


# Plugin name -> probe. Membership answers "can this plugin's use be measured
# at all", which is a static fact and stays true even when a probe fails at
# runtime. Adding an entry requires the sole-writer proof in the module
# docstring; without it the reading would attribute another plugin's work here.
_PROBES: dict[str, Callable[..., PluginActivity]] = {
    "headroom": _headroom_activity,
    "mem0": _mem0_activity,
    "obsidian": _obsidian_activity,
    "hindsight": _hindsight_activity,
}


def probed_plugins() -> frozenset[str]:
    """Names whose activity is measurable. Everything else is presence-only."""
    return frozenset(_PROBES)


def activity_for(
    name: str,
    *,
    tenant: str | None = "default",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> PluginActivity | None:
    """Activity for plugin *name*, or ``None`` when it cannot be read.

    ``None`` covers two cases the caller separates via `probed_plugins`: a
    plugin with no telemetry at all (absent from the registry), and a probe
    that failed (present, but the read raised). Never raises — a broken metrics
    table must not take plugin health down with it.
    """
    probe = _PROBES.get(name)
    if probe is None:
        return None
    try:
        return probe(tenant=tenant, window_days=window_days)
    except Exception as exc:  # noqa: BLE001 — health must not 500 on a bad read
        logger.warning("plugin_activity.probe_failed", plugin=name, error=str(exc))
        return None
