"""
Read-only aggregate analytics over the existing SQLite/Postgres run store
(Phase 24a — SLA / duration / volume analytics).

Every public function here is:
- **Read-only.** No table is ever written to.
- **Tenant-filtered**, mirroring `state_service.list_recent_runs(tenant=...)`.
  Pass ``tenant=None`` for an unfiltered (all-tenant/admin) view.
- **Time-windowed** via ``days`` (relative window, default 30) or an explicit
  ``since``/``until`` pair. Passing ``days=None`` with no ``since``/``until``
  means "unbounded" (all history).

Timestamp handling
-------------------
`runs.started_at` / `runs.finished_at` / `steps.timestamp` /
`approvals.requested_at` / `approvals.approved_at` are all stored via
SQLite's ``DEFAULT CURRENT_TIMESTAMP``, which yields the fixed-width,
lexicographically-sortable UTC format ``"YYYY-MM-DD HH:MM:SS"``. Because the
format is fixed-width and zero-padded, plain string comparison in SQL
(``>=`` / ``<=``) is a correct and portable substitute for a proper
timestamp comparison across both SQLite and Postgres, and
``datetime.fromisoformat()`` parses it directly (Python 3.11+ accepts the
space date/time separator; the project's CI runs 3.12).

Percentile method
------------------
SQLite has no percentile aggregate, so percentiles are computed in Python
from the fetched duration list using the **nearest-rank method**:
for a sorted list of ``n`` values and percentile ``p`` (0-100)::

    rank  = ceil(p / 100 * n)
    index = clamp(rank - 1, 0, n - 1)
    percentile = sorted_values[index]

This is deterministic, has no interpolation ambiguity, and always returns an
observed value from the sample (never a synthetic interpolated number).

Canonical outcome mapping
--------------------------
The `runs.status` column has historically mixed a legacy literal
(``"success"``) with the formal `state_service.RunStatus` enum
(``RunStatus.COMPLETE == "complete"``), plus ad-hoc literals written by the
orchestrator (``"failed"``, ``"denied"``, ``"deferred"``, ...). All outcome
bucketing in this module goes through :func:`canonical_outcome`, the single
source of truth for the status -> outcome mapping. `hivepilot.ui.dashboard`
imports and reuses the same mapping so the Textual dashboard and this API
never disagree about what counts as a "successful" run.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from hivepilot.services import db, state_service

# ---------------------------------------------------------------------------
# Canonical outcome mapping
# ---------------------------------------------------------------------------


class Outcome(str, Enum):
    """Canonical outcome buckets used by every aggregate in this module."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    OTHER = "other"


# Legacy literal "success" (written by hivepilot/orchestrator.py) and the
# formal RunStatus.COMPLETE value both mean "the run finished successfully".
_SUCCEEDED_STATUSES = {"success", "complete"}

# "failed" (legacy literal) + "denied" (approval workflow rejection) + the
# formal RunStatus failure states.
_FAILED_STATUSES = {
    "failed",
    "denied",
    "rate_limit",
    "auth_expired",
    "test_failure",
    "security_blocker",
}

# "deferred" (quota/backoff — scheduler.retry_service re-queues it later,
# it was not executed to completion or failure this cycle).
_SKIPPED_STATUSES = {"deferred"}

# Everything else (running, pending, new, planned, paused, review, approval,
# awaiting_approval, ...) is a non-terminal or unrecognized state -> "other".


def canonical_outcome(status: str | None) -> str:
    """Map a raw ``runs.status`` (or ``steps.status``) value to a canonical
    outcome bucket: ``"succeeded"``, ``"failed"``, ``"skipped"``, or
    ``"other"``. Case-insensitive; ``None`` maps to ``"other"``.
    """
    if status is None:
        return Outcome.OTHER.value
    normalised = status.strip().lower()
    if normalised in _SUCCEEDED_STATUSES:
        return Outcome.SUCCEEDED.value
    if normalised in _FAILED_STATUSES:
        return Outcome.FAILED.value
    if normalised in _SKIPPED_STATUSES:
        return Outcome.SKIPPED.value
    return Outcome.OTHER.value


# ---------------------------------------------------------------------------
# Time-window resolution
# ---------------------------------------------------------------------------

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_window(
    days: int | None, since: str | None, until: str | None
) -> tuple[str | None, str | None]:
    """Return a ``(since, until)`` pair of SQL-comparable timestamp strings.

    - If either ``since`` or ``until`` is given explicitly, use them as-is
      (unbounded on the side that's omitted).
    - Otherwise, if ``days`` is given, the window is ``[now - days, now]``
      (``until`` left unbounded — "now" isn't compared to avoid clock-skew
      false negatives against a `finished_at` written a moment later).
    - If ``days`` is ``None`` and no explicit bounds are given, the window
      is fully unbounded (all history).
    """
    if since is not None or until is not None:
        return since, until
    if days is None:
        return None, None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime(_TS_FORMAT), None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Percentiles (nearest-rank method — see module docstring)
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    rank = math.ceil((pct / 100.0) * n)
    idx = max(0, min(n - 1, rank - 1))
    return sorted_values[idx]


def _duration_stats(durations: list[float]) -> dict[str, float]:
    if not durations:
        return {"count": 0, "min": 0.0, "max": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    ordered = sorted(durations)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "avg": round(sum(ordered) / len(ordered), 3),
        "p50": round(_percentile(ordered, 50), 3),
        "p95": round(_percentile(ordered, 95), 3),
        "p99": round(_percentile(ordered, 99), 3),
    }


# ---------------------------------------------------------------------------
# Run fetch helper
# ---------------------------------------------------------------------------


def _query_runs(
    tenant: str | None,
    project: str | None,
    task: str | None,
    since: str | None,
    until: str | None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if tenant is not None:
        clauses.append("tenant=?")
        params.append(tenant)
    if project is not None:
        clauses.append("project=?")
        params.append(project)
    if task is not None:
        clauses.append("task=?")
        params.append(task)
    if since is not None:
        clauses.append("started_at>=?")
        params.append(since)
    if until is not None:
        clauses.append("started_at<=?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM runs {where} ORDER BY started_at"
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [dict(row) for row in rows]


def _outcome_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {o.value: 0 for o in Outcome}
    for run in runs:
        counts[canonical_outcome(run.get("status"))] += 1
    return counts


def _outcome_rates(counts: dict[str, int], total: int) -> dict[str, float]:
    if total == 0:
        return {k: 0.0 for k in counts}
    return {k: round(v / total, 4) for k, v in counts.items()}


def _group_by(runs: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[run.get(key) or "unknown"].append(run)
    return dict(groups)


def _group_outcome_summary(runs: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for group_key, group_runs in _group_by(runs, key).items():
        counts = _outcome_counts(group_runs)
        result[group_key] = {
            "total": len(group_runs),
            "outcomes": counts,
            "outcome_rates": _outcome_rates(counts, len(group_runs)),
        }
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_summary(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """Totals + outcome rates overall, and grouped by project/task/raw status."""
    state_service.init_db()
    since_ts, until_ts = _resolve_window(days, since, until)
    runs = _query_runs(tenant, project, task, since_ts, until_ts)
    total = len(runs)
    outcomes = _outcome_counts(runs)
    raw_status_counts: dict[str, int] = defaultdict(int)
    for run in runs:
        raw_status_counts[run.get("status") or "unknown"] += 1
    return {
        "total": total,
        "outcomes": outcomes,
        "outcome_rates": _outcome_rates(outcomes, total),
        "by_project": _group_outcome_summary(runs, "project"),
        "by_task": _group_outcome_summary(runs, "task"),
        "by_raw_status": dict(raw_status_counts),
    }


def _bucket_key(started_at: str | None, bucket: str) -> str | None:
    dt = _parse_ts(started_at)
    if dt is None:
        return None
    if bucket == "day":
        return dt.strftime("%Y-%m-%d")
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def run_trends(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
    bucket: str = "day",
) -> dict[str, Any]:
    """Time-series run counts (+ outcome split), bucketed on `started_at`."""
    if bucket not in ("day", "week"):
        raise ValueError(f"bucket must be 'day' or 'week', got {bucket!r}")
    state_service.init_db()
    since_ts, until_ts = _resolve_window(days, since, until)
    runs = _query_runs(tenant, project, task, since_ts, until_ts)

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        key = _bucket_key(run.get("started_at"), bucket)
        if key is not None:
            buckets[key].append(run)

    series = []
    for key in sorted(buckets):
        group_runs = buckets[key]
        series.append(
            {
                "bucket": key,
                "total": len(group_runs),
                "outcomes": _outcome_counts(group_runs),
            }
        )
    return {"bucket": bucket, "series": series}


def _durations_seconds(runs: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for run in runs:
        start = _parse_ts(run.get("started_at"))
        end = _parse_ts(run.get("finished_at"))
        if start is None or end is None:
            continue
        delta = (end - start).total_seconds()
        if delta < 0:
            continue
        out.append(delta)
    return out


def run_durations(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """p50/p95/p99 + min/max/avg duration (finished_at - started_at) for
    finished runs only, overall and grouped by project/task."""
    state_service.init_db()
    since_ts, until_ts = _resolve_window(days, since, until)
    runs = _query_runs(tenant, project, task, since_ts, until_ts)
    finished = [r for r in runs if r.get("finished_at")]

    overall = _duration_stats(_durations_seconds(finished))
    by_project = {
        key: _duration_stats(_durations_seconds(group))
        for key, group in _group_by(finished, "project").items()
    }
    by_task = {
        key: _duration_stats(_durations_seconds(group))
        for key, group in _group_by(finished, "task").items()
    }
    return {"overall": overall, "by_project": by_project, "by_task": by_task}


def step_failure_hotspots(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """`steps` grouped by (step, status), ranked with the highest-failure-count
    combinations first (ties broken by count, descending)."""
    state_service.init_db()
    since_ts, until_ts = _resolve_window(days, since, until)

    clauses: list[str] = []
    params: list[Any] = []
    if tenant is not None:
        clauses.append("r.tenant=?")
        params.append(tenant)
    if project is not None:
        clauses.append("r.project=?")
        params.append(project)
    if task is not None:
        clauses.append("r.task=?")
        params.append(task)
    if since_ts is not None:
        clauses.append("s.timestamp>=?")
        params.append(since_ts)
    if until_ts is not None:
        clauses.append("s.timestamp<=?")
        params.append(until_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT s.step AS step, s.status AS status
        FROM steps s
        JOIN runs r ON r.id = s.run_id
        {where}
    """
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        counts[(row["step"], row["status"])] += 1

    hotspots: list[dict[str, Any]] = [
        {"step": step, "status": status, "count": count} for (step, status), count in counts.items()
    ]
    hotspots.sort(
        key=lambda h: (0 if canonical_outcome(h["status"]) == "failed" else 1, -h["count"])
    )
    return hotspots[:limit]


def _steps_grouped_by(
    column: str,
    tenant: str | None,
    days: int | None,
    since: str | None,
    until: str | None,
    project: str | None,
    task: str | None,
) -> list[dict[str, Any]]:
    """Shared query for `steps_by_provider`/`steps_by_model` (Phase 24b.1):
    `steps` rows joined to `runs` for tenant scoping (mirrors
    `step_failure_hotspots`), grouped by *column* (``"provider"`` or
    ``"model"``), with counts + outcome split via `canonical_outcome`.

    A ``NULL`` value in *column* (a step whose provider/model was genuinely
    unknown at record time — e.g. a shell runner has no model, or a step
    recorded before this sprint's migration) groups under the literal key
    ``"unknown"``, never dropped and never invented as a real provider/model
    name. Results are sorted by descending total (most-used first).
    """
    if column not in ("provider", "model"):
        raise ValueError(f"column must be 'provider' or 'model', got {column!r}")
    state_service.init_db()
    since_ts, until_ts = _resolve_window(days, since, until)

    clauses: list[str] = []
    params: list[Any] = []
    if tenant is not None:
        clauses.append("r.tenant=?")
        params.append(tenant)
    if project is not None:
        clauses.append("r.project=?")
        params.append(project)
    if task is not None:
        clauses.append("r.task=?")
        params.append(task)
    if since_ts is not None:
        clauses.append("s.timestamp>=?")
        params.append(since_ts)
    if until_ts is not None:
        clauses.append("s.timestamp<=?")
        params.append(until_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT s.{column} AS grouping_key, s.status AS status
        FROM steps s
        JOIN runs r ON r.id = s.run_id
        {where}
    """
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()

    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        key = row["grouping_key"] or "unknown"
        grouped[key].append(row["status"])

    result: list[dict[str, Any]] = []
    for key, statuses in grouped.items():
        counts = {o.value: 0 for o in Outcome}
        for status in statuses:
            counts[canonical_outcome(status)] += 1
        total = len(statuses)
        result.append(
            {
                column: key,
                "total": total,
                "outcomes": counts,
                "outcome_rates": _outcome_rates(counts, total),
            }
        )
    result.sort(key=lambda r: -r["total"])
    return result


def steps_by_provider(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> list[dict[str, Any]]:
    """`steps` grouped by `provider` (the runner kind or resolved API
    provider — see `hivepilot.orchestrator._resolve_step_provider_model`),
    with counts + outcome split. Steps with no recorded provider group under
    ``"unknown"``."""
    return _steps_grouped_by("provider", tenant, days, since, until, project, task)


def steps_by_model(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> list[dict[str, Any]]:
    """`steps` grouped by `model`, with counts + outcome split. Steps with no
    recorded model (e.g. a shell runner) group under ``"unknown"``."""
    return _steps_grouped_by("model", tenant, days, since, until, project, task)


def approval_latency(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """p50/p95 (+ min/max/avg/count) of `approved_at - requested_at` for
    approvals that have been actioned (pending approvals are excluded)."""
    state_service.init_db()
    since_ts, until_ts = _resolve_window(days, since, until)

    clauses: list[str] = ["approved_at IS NOT NULL"]
    params: list[Any] = []
    if tenant is not None:
        clauses.append("tenant=?")
        params.append(tenant)
    if project is not None:
        clauses.append("project=?")
        params.append(project)
    if task is not None:
        clauses.append("task=?")
        params.append(task)
    if since_ts is not None:
        clauses.append("requested_at>=?")
        params.append(since_ts)
    if until_ts is not None:
        clauses.append("requested_at<=?")
        params.append(until_ts)
    where = f"WHERE {' AND '.join(clauses)}"
    sql = f"SELECT requested_at, approved_at FROM approvals {where}"
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()

    durations: list[float] = []
    for row in rows:
        start = _parse_ts(row["requested_at"])
        end = _parse_ts(row["approved_at"])
        if start is None or end is None:
            continue
        delta = (end - start).total_seconds()
        if delta < 0:
            continue
        durations.append(delta)

    return _duration_stats(durations)
