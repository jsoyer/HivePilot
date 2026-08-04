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

from hivepilot.services import db, pricing, state_service

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


def _attempt_success_rate(counts: dict[str, int]) -> float | None:
    """``succeeded / (succeeded + failed)`` -- i.e. the success rate among
    runs that were actually *attempted* to completion.

    Unlike ``_outcome_rates()["succeeded"]`` (which divides by *every* run,
    including SKIPPED/OTHER), this denominator deliberately EXCLUDES
    SKIPPED and OTHER: a run that was deferred/skipped never got a chance
    to succeed or fail, so it must not silently deflate the success rate
    toward 0%. A group that is 100% SKIPPED has zero attempts, so this
    returns ``None`` (never ``0.0``) -- the caller/consumer can then render
    a distinct "skipped" signal instead of a misleading "0% success".
    """
    succeeded = counts.get(Outcome.SUCCEEDED.value, 0)
    failed = counts.get(Outcome.FAILED.value, 0)
    attempts = succeeded + failed
    if attempts == 0:
        return None
    return round(succeeded / attempts, 4)


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
            "success_rate": _attempt_success_rate(counts),
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
        "success_rate": _attempt_success_rate(outcomes),
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


# A step only belongs in a MODEL view if it actually invoked a model. Two
# kinds of row do not: a `shell` runner (`groomer-scan`'s `signals` step runs
# `hivepilot drift scan`, no LLM involved) and a `skip:<stage>` bookkeeping
# row written for a stage that never executed.
#
# Counting them produced a `unknown` pseudo-model with 234 steps and a 9%
# "success rate" that belonged to neither a model nor an agent. They are now
# excluded from model grouping and REPORTED as a separate count -- silently
# dropping rows would trade one wrong number for another.
# Imported, not redeclared: `record_step` refuses to attribute these very
# providers, so classification here must use the same set it was recorded
# under. Two copies would eventually disagree, and the disagreement would
# surface as spend belonging to no one.
_NON_MODEL_PROVIDERS = state_service.NON_MODEL_PROVIDERS


def _steps_grouped_by(
    column: str,
    tenant: str | None,
    days: int | None,
    since: str | None,
    until: str | None,
    project: str | None,
    task: str | None,
    model_invocations_only: bool = False,
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
    if model_invocations_only:
        placeholders = ", ".join("?" for _ in _NON_MODEL_PROVIDERS)
        # NULL provider is KEPT: it is a genuine telemetry gap and must stay
        # visible. Only rows that cannot have invoked a model are removed.
        clauses.append(f"(s.provider IS NULL OR s.provider NOT IN ({placeholders}))")
        params.extend(sorted(_NON_MODEL_PROVIDERS))
        clauses.append("s.step NOT LIKE 'skip:%'")
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
                "success_rate": _attempt_success_rate(counts),
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
    """`steps` grouped by `model`, restricted to steps that actually invoked
    one — see `_NON_MODEL_PROVIDERS`.

    A step that reaches a model but records none (9 rows in the reference
    deployment: `provider='claude'`, `model IS NULL`) still groups under
    ``"unknown"``. That is a genuine telemetry gap and must stay visible; it
    is not the same thing as a shell command, which is why the shell rows are
    now filtered out rather than pooled with it."""
    return _steps_grouped_by(
        "model", tenant, days, since, until, project, task, model_invocations_only=True
    )


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


# ---------------------------------------------------------------------------
# cost_summary (Phase 24b.2b) — cost/provider analytics on top of the
# Phase 24b.1 provider/model columns and the Phase 24b.2a usage columns.
# ---------------------------------------------------------------------------


def _step_cost(row: dict[str, Any]) -> tuple[float, bool]:
    """Effective cost for one `steps` row + whether it was priced at all.

    Precedence: self-reported ``cost_usd`` (authoritative) > an estimate from
    the price map (`pricing.estimate_cost`, INCLUDING cache_read_tokens/
    cache_creation_tokens -- usage-capture-modelusage fix: cache tokens are
    billed and must never be silently dropped from the estimate) > unpriced
    (contributes 0.0 to the cost total but is flagged so callers never
    silently present a total that omits unpriced steps as if it were
    complete).
    """
    cost_usd = row.get("cost_usd")
    if cost_usd is not None:
        return float(cost_usd), True
    estimated = pricing.estimate_cost(
        row.get("model"),
        row.get("input_tokens"),
        row.get("output_tokens"),
        cache_read_tokens=row.get("cache_read_tokens") or 0,
        cache_creation_tokens=row.get("cache_creation_tokens") or 0,
    )
    if estimated is not None:
        return estimated, True
    return 0.0, False


def _unpriced_reason(row: dict[str, Any]) -> str:
    """Why this step has no cost — the price map, or the usage capture.

    The dashboard warned *"N model(s) have no pricing data on record — total
    cost is understated"* and pointed the operator at
    `HIVEPILOT_LLM_PRICE_MAP`. On the reference deployment that was wrong for
    every model it named: `opus`, `sonnet` and `haiku` ARE in the price map,
    and their unpriced steps correlate exactly with steps that recorded no
    tokens (16/16, 21/21, 1/1). Nothing was missing from the map; the usage
    capture never ran. Blaming the wrong subsystem sends the fix to the wrong
    place, so the two causes are now separated.
    """
    model = row.get("model")
    if not model:
        return "no_model_recorded"
    if model not in pricing._effective_price_map():
        return "no_price_for_model"
    if not (row.get("input_tokens") or row.get("output_tokens")):
        return "no_usage_captured"
    return "no_price_for_model"


def _is_model_invocation(row: dict[str, Any]) -> bool:
    """True when this step could have called a model at all.

    A `shell` runner and a `skip:<stage>` bookkeeping row could not — see
    `_NON_MODEL_PROVIDERS`.
    """
    provider = row.get("provider")
    if provider in _NON_MODEL_PROVIDERS:
        return False
    step = row.get("step") or ""
    return not step.startswith("skip:")


def _accumulate_cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over *rows*, splitting UNPRICED from UNPRICEABLE.

    A step run by a `shell` runner, or a `skip:<stage>` bookkeeping row, never
    invoked a model: it has no token cost to miss. Counting it alongside a
    genuine pricing gap is what made the dashboard warn *"total cost is
    understated"* about 271 steps when almost none of them could have cost
    anything. The rows are still counted in `total_steps` — nothing is
    dropped — but only `unpriced_steps` justifies that warning.
    """
    total_cost = 0.0
    total_input = 0
    total_output = 0
    unpriced_steps = 0
    unpriceable_steps = 0
    reasons: dict[str, int] = defaultdict(int)
    for row in rows:
        cost, priced = _step_cost(row)
        total_cost += cost
        total_input += row.get("input_tokens") or 0
        total_output += row.get("output_tokens") or 0
        if priced:
            continue
        if not _is_model_invocation(row):
            unpriceable_steps += 1
            continue
        unpriced_steps += 1
        reasons[_unpriced_reason(row)] += 1
    return {
        "total_steps": len(rows),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": round(total_cost, 6),
        "unpriced_steps": unpriced_steps,
        "unpriceable_steps": unpriceable_steps,
        "unpriced_reasons": dict(reasons),
    }


# `steps.role` (added by the Mirador Agent Panels backend sprint) is
# populated only for steps recorded from this sprint onward -- every step
# written before the migration ships (and any step from a non-role task)
# persists role=NULL. `by_role` below groups those under the literal key
# "unknown" -- same never-drop, never-guess convention as `by_provider`/
# `by_model` grouping a NULL provider/model under "unknown".
_BY_ROLE_NOTE = (
    "by_role groups steps by the role that executed them (steps.role, added "
    "in the Mirador Agent Panels backend sprint). Steps recorded BEFORE that "
    "migration shipped -- and any step from a non-role task -- have "
    "role=NULL and are grouped under the literal key 'unknown', never "
    "dropped and never attributed to a guessed role."
)


def session_costs(
    tenant: str | None = None,
    days: int | None = 30,
    limit: int = 25,
) -> dict[str, Any]:
    """Per-run cost, split by what was actually billed.

    A total alone cannot answer "where did the money go", and on this
    workload the intuitive reading is the wrong one: one review dispatch
    recorded 516 982 cache-read tokens against 3 040 fresh input and 20 455
    output. Read as volume that says the reviewers read too much; read as
    cost it says they write a lot and the reading is cached and cheap. Only
    the second is true, and only the split shows it.

    Each session reports token counts AND the cost each component carries
    (`pricing.cost_components`). Steps whose model has no price on record —
    or has cache volume it holds no rate for — are counted in
    ``unpriced_steps`` rather than silently contributing zero: a session
    that is partly unpriceable must not look cheap.
    """
    state_service.init_db()
    since_ts, until_ts = _resolve_window(days, None, None)

    clauses: list[str] = []
    params: list[Any] = []
    if tenant is not None:
        clauses.append("r.tenant=?")
        params.append(tenant)
    if since_ts is not None:
        clauses.append("s.timestamp>=?")
        params.append(since_ts)
    if until_ts is not None:
        clauses.append("s.timestamp<=?")
        params.append(until_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with db.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                db.ph(
                    "SELECT s.run_id AS run_id, r.project AS project, r.task AS task, "
                    "r.started_at AS started_at, r.status AS status, s.model AS model, "
                    "s.input_tokens AS input_tokens, s.output_tokens AS output_tokens, "
                    "s.cache_read_tokens AS cache_read_tokens, "
                    "s.cache_creation_tokens AS cache_creation_tokens "
                    f"FROM steps s JOIN runs r ON r.id = s.run_id {where}"
                ),
                tuple(params),
            ).fetchall()
        ]

    sessions: dict[int, dict[str, Any]] = {}
    for row in rows:
        run_id = int(row["run_id"])
        entry = sessions.setdefault(
            run_id,
            {
                "run_id": run_id,
                "project": row["project"],
                "task": row["task"],
                "started_at": row["started_at"],
                "status": row["status"],
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_usd": 0.0,
                "unpriced_steps": 0,
                "by_component": {
                    "input": 0.0,
                    "output": 0.0,
                    "cache_read": 0.0,
                    "cache_write": 0.0,
                },
            },
        )
        for field, key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_tokens", "cache_read_tokens"),
            ("cache_creation_tokens", "cache_creation_tokens"),
        ):
            entry[key] += int(row.get(field) or 0)

        parts = pricing.cost_components(
            row.get("model"),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cache_read_tokens=row.get("cache_read_tokens") or 0,
            cache_creation_tokens=row.get("cache_creation_tokens") or 0,
        )
        if parts is None:
            # Only count a step as unpriceable when it plausibly cost
            # something. A shell step with no tokens is not a pricing gap.
            if row.get("input_tokens") or row.get("output_tokens"):
                entry["unpriced_steps"] += 1
            continue
        for component, value in parts.items():
            entry["by_component"][component] += value
        entry["cost_usd"] += sum(parts.values())

    ordered = sorted(sessions.values(), key=lambda s: -s["cost_usd"])[:limit]
    for entry in ordered:
        entry["cost_usd"] = round(entry["cost_usd"], 6)
        entry["by_component"] = {k: round(v, 6) for k, v in entry["by_component"].items()}
    return {"sessions": ordered, "total_sessions": len(sessions)}


def cost_summary(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """Cost/token totals, overall and grouped by `provider`, `model`, and
    `project`.

    Tenant-scoped via `steps JOIN runs` (mirrors `_steps_grouped_by`). A
    `NULL` provider/model groups under the literal key ``"unknown"`` — never
    dropped, never invented (a run's `project` is a required field, so no
    `NULL` case exists there). Each group (and the overall total) reports
    both the summed cost AND ``unpriced_steps`` — a count of steps that had
    no cost signal at all (no self-reported `cost_usd` and no price-map
    match), so a dashboard can show coverage instead of presenting a total
    that silently omits unpriced steps as if it were complete.
    ``unpriced_models`` lists which model names (from ``by_model``) have at
    least one unpriced step, so a dashboard can point at exactly what's
    missing from the price map.

    ``by_role`` groups the same steps by `steps.role` (Pollen Agent Panels
    backend sprint) -- see `_BY_ROLE_NOTE` (paired with ``by_role_note`` in
    the return value). A `NULL` role (every step recorded before that
    migration shipped, plus any non-role task's steps) groups under the
    literal key ``"unknown"``, exactly like `by_provider`/`by_model`.
    """
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
    # Same exclusion as the model view: a shell command and a skipped stage
    # have no token cost, and counting them as "unpriced steps" turned a
    # complete picture into a warning about 271 missing prices.
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT s.provider AS provider, s.model AS model, r.project AS project,
               s.role AS role, s.step AS step,
               s.input_tokens AS input_tokens, s.output_tokens AS output_tokens,
               s.cache_read_tokens AS cache_read_tokens,
               s.cache_creation_tokens AS cache_creation_tokens,
               s.cost_usd AS cost_usd
        FROM steps s
        JOIN runs r ON r.id = s.run_id
        {where}
    """
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute(db.ph(sql), tuple(params)).fetchall()]

    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_provider[row.get("provider") or "unknown"].append(row)
        by_model[row.get("model") or "unknown"].append(row)
        by_role[row.get("role") or "unknown"].append(row)

    provider_summary = [
        {"provider": key, **_accumulate_cost(group)} for key, group in by_provider.items()
    ]
    provider_summary.sort(key=lambda r: -r["cost_usd"])
    model_summary = [{"model": key, **_accumulate_cost(group)} for key, group in by_model.items()]
    model_summary.sort(key=lambda r: -r["cost_usd"])
    role_summary = [{"role": key, **_accumulate_cost(group)} for key, group in by_role.items()]
    role_summary.sort(key=lambda r: -r["cost_usd"])

    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_project[row.get("project") or "unknown"].append(row)
    project_summary = [
        {"project": key, **_accumulate_cost(group)} for key, group in by_project.items()
    ]
    project_summary.sort(key=lambda r: -r["cost_usd"])
    unpriced_models = sorted(row["model"] for row in model_summary if row["unpriced_steps"] > 0)

    return {
        "overall": _accumulate_cost(rows),
        "by_provider": provider_summary,
        "by_model": model_summary,
        "by_project": project_summary,
        "by_role": role_summary,
        "by_role_note": _BY_ROLE_NOTE,
        "unpriced_models": unpriced_models,
    }


# ---------------------------------------------------------------------------
# models_summary (Pollen data endpoints sprint) — per-model rollup for
# GET /v1/models: cost, tokens, step count, success rate, share of spend,
# and an overall cost-per-successful-run figure.
# ---------------------------------------------------------------------------


_LATENCY_UNAVAILABLE_NOTE = (
    "p50/p95 latency is not computable from current data and is intentionally "
    "omitted rather than fabricated: steps.timestamp is a single point-in-time "
    "value (no per-step start/end pair -- see state_service.record_step()), and "
    "runs.started_at/finished_at describe the WHOLE run, which can span "
    "multiple steps/models -- attributing a run's duration to just one of its "
    "models would misattribute latency. Add a steps.duration_seconds (or "
    "started_at/finished_at pair) column to make this measurable."
)


def models_summary(
    tenant: str | None = None,
    days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """Per-model rollup backing `GET /v1/models`: cost, tokens, step count,
    success rate, and each model's share of total in-window spend, plus an
    overall cost-per-successful-run figure.

    Reuses the same `steps JOIN runs` query shape as `cost_summary`/
    `_steps_grouped_by` — tenant-scoped, `NULL` model groups under
    ``"unknown"``. ``success_rate`` uses `_attempt_success_rate` (excludes
    skipped/other from the denominator — see that function's docstring).
    ``share_of_spend`` is each model's ``cost_usd`` divided by the in-window
    total across all models (``0.0`` when the total is ``0.0``, never a
    ``ZeroDivisionError``). Sorted by descending cost (biggest spender
    first).

    ``overall.cost_per_successful_run`` divides the in-window total cost by
    the count of RUNS (not steps) whose canonical outcome is "succeeded" in
    the same window (`runs.started_at`-scoped, mirroring `run_summary`) —
    ``None`` when there are zero succeeded runs (never a misleading `0.0`,
    mirrors `_attempt_success_rate`'s own "no attempts -> None" contract).

    Latency (p50/p95): NOT computable from current data — see
    `_LATENCY_UNAVAILABLE_NOTE` (``latency_available=False`` +
    ``latency_note`` in the return value) — intentionally omitted per model
    rather than fabricated.
    """
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
        SELECT s.model AS model, s.status AS status,
               s.input_tokens AS input_tokens, s.output_tokens AS output_tokens,
               s.cache_read_tokens AS cache_read_tokens,
               s.cache_creation_tokens AS cache_creation_tokens,
               s.cost_usd AS cost_usd
        FROM steps s
        JOIN runs r ON r.id = s.run_id
        {where}
    """
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute(db.ph(sql), tuple(params)).fetchall()]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("model") or "unknown"].append(row)

    overall_cost = _accumulate_cost(rows)
    total_cost = overall_cost["cost_usd"]

    models: list[dict[str, Any]] = []
    for key, group in grouped.items():
        cost_stats = _accumulate_cost(group)
        outcome_counts = {o.value: 0 for o in Outcome}
        for row in group:
            outcome_counts[canonical_outcome(row.get("status"))] += 1
        models.append(
            {
                "model": key,
                "step_count": cost_stats["total_steps"],
                "input_tokens": cost_stats["input_tokens"],
                "output_tokens": cost_stats["output_tokens"],
                "cost_usd": cost_stats["cost_usd"],
                "unpriced_steps": cost_stats["unpriced_steps"],
                "success_rate": _attempt_success_rate(outcome_counts),
                "share_of_spend": round(cost_stats["cost_usd"] / total_cost, 4)
                if total_cost > 0
                else 0.0,
            }
        )
    models.sort(key=lambda r: -r["cost_usd"])

    runs = _query_runs(tenant, project, task, since_ts, until_ts)
    run_outcomes = _outcome_counts(runs)
    succeeded_runs = run_outcomes[Outcome.SUCCEEDED.value]
    cost_per_successful_run = round(total_cost / succeeded_runs, 6) if succeeded_runs > 0 else None

    return {
        "models": models,
        "overall": {
            **overall_cost,
            "succeeded_runs": succeeded_runs,
            "cost_per_successful_run": cost_per_successful_run,
        },
        "latency_available": False,
        "latency_note": _LATENCY_UNAVAILABLE_NOTE,
    }


# ---------------------------------------------------------------------------
# agents_summary (Mirador Agent Panels backend sprint) — per-role activity
# roster for GET /v1/agents: the FULL role roster (hivepilot.roles.ROLES)
# left-joined with REAL per-role activity derived from `steps.role`.
# ---------------------------------------------------------------------------

_AGENTS_ATTRIBUTION_NOTE = (
    "Per-role attribution requires steps.role, added in the Pollen Agent "
    "Panels backend sprint. Only steps recorded AFTER that migration ships "
    "carry a real role -- earlier steps (and any step from a non-role task) "
    "have role=NULL and are reported separately under the 'unknown' bucket, "
    "never guessed and never silently dropped. A role with zero attributed "
    "steps in the current window is returned with attributed=false and "
    "success_rate=null ('no data yet'), not a fabricated rollup. No latency "
    "figure is computed here (see `_LATENCY_UNAVAILABLE_NOTE` -- the same "
    "per-step-duration gap applies per role)."
)


def _agent_role_stats(group: list[dict[str, Any]]) -> dict[str, Any]:
    cost_stats = _accumulate_cost(group)
    outcome_counts = {o.value: 0 for o in Outcome}
    for row in group:
        outcome_counts[canonical_outcome(row.get("status"))] += 1
    timestamps = [row["timestamp"] for row in group if row.get("timestamp")]
    return {
        "attributed": bool(group),
        "run_count": len({row["run_id"] for row in group}),
        "step_count": cost_stats["total_steps"],
        "input_tokens": cost_stats["input_tokens"],
        "output_tokens": cost_stats["output_tokens"],
        "cost_usd": cost_stats["cost_usd"],
        "unpriced_steps": cost_stats["unpriced_steps"],
        "success_rate": _attempt_success_rate(outcome_counts),
        "last_active": max(timestamps) if timestamps else None,
    }


# The three reasons a step can carry no role. Always all present in the
# payload, at zero when empty — a missing key would read as "this cannot
# happen here" rather than "this did not happen".
_UNKNOWN_REASONS = ("no_model", "skipped", "attribution_gap")


def _unknown_reason(row: dict[str, Any]) -> str:
    """Why this row has no role.

    Only ``attribution_gap`` is a defect. The other two are structural and
    correctly excluded from per-role figures:

    - ``skipped`` — the step never ran, so it invoked nothing. Checked first:
      a skip is a skip whatever provider was declared for it.
    - ``no_model`` — a non-model provider (`shell`), which cannot have a role
      because no agent was involved. Uses the same `_NON_MODEL_PROVIDERS`
      definition as `_steps_grouped_by`, so "not a model invocation" means
      one thing across analytics.
    - ``attribution_gap`` — a model ran (or the provider is NULL, a genuine
      telemetry gap) and no role was recorded. This is spend and work missing
      from every per-agent figure, and the only number here worth acting on.
      NULL-provider non-skip rows land here deliberately: an unknown is not
      evidence of harmlessness.
    """
    if str(row.get("step") or "").startswith("skip:"):
        return "skipped"
    provider = row.get("provider")
    if provider is not None and provider in _NON_MODEL_PROVIDERS:
        return "no_model"
    return "attribution_gap"


def _unknown_breakdown(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Split the NULL-role bucket by cause, with the cost each carries.

    Every row lands in exactly one bucket, so the counts sum to the bucket's
    own `step_count` — a breakdown that dropped rows would understate the
    attribution gap without saying so.
    """
    breakdown: dict[str, dict[str, Any]] = {
        reason: {"step_count": 0, "cost_usd": 0.0} for reason in _UNKNOWN_REASONS
    }
    for row in rows:
        part = breakdown[_unknown_reason(row)]
        part["step_count"] += 1
        part["cost_usd"] += float(row.get("cost_usd") or 0.0)
    for part in breakdown.values():
        part["cost_usd"] = round(part["cost_usd"], 6)
    return breakdown


def agents_summary(
    tenant: str | None = None,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """Per-role agent activity roster backing `GET /v1/agents` (Pollen
    Agent Panels backend sprint): the full role roster from
    `hivepilot.roles.list_roles()` (name/display_name/title), LEFT-JOINed
    with real per-role activity derived from `steps.role`.

    Tenant-scoped exactly like `cost_summary`/`models_summary` (`steps JOIN
    runs`, `r.tenant=?`). Unbounded by default (``days=None``) — a roster
    view is a lifetime/overview surface, not a rolling-window one, but the
    same `days`/`since`/`until` knobs as every other analytics function are
    still honored when a caller wants a windowed slice.

    Honesty contract:
    - A role with zero attributed steps in the window returns
      ``attributed: False``, all-zero counts, and ``success_rate: None``
      ("no data yet") -- never a fabricated rollup.
    - `NULL`-role steps (pre-migration history, or a non-role task) are
      NEVER attributed to any named role -- they're aggregated separately
      under the top-level ``"unknown"`` key.
    - A role name observed in the data but no longer present in the current
      roster (e.g. `roles.yaml` changed) is still surfaced in ``agents``
      (with ``display_name``/``title`` both ``None``) rather than silently
      dropped.
    - No latency figure is ever computed/returned per role -- see
      `_AGENTS_ATTRIBUTION_NOTE`.
    """
    from hivepilot import roles as roles_module

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
        SELECT s.role AS role, s.run_id AS run_id, s.status AS status,
               s.step AS step, s.provider AS provider,
               s.input_tokens AS input_tokens, s.output_tokens AS output_tokens,
               s.cache_read_tokens AS cache_read_tokens,
               s.cache_creation_tokens AS cache_creation_tokens,
               s.cost_usd AS cost_usd, s.timestamp AS timestamp
        FROM steps s
        JOIN runs r ON r.id = s.run_id
        {where}
    """
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute(db.ph(sql), tuple(params)).fetchall()]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("role") or "unknown"].append(row)
    unknown_rows = grouped.pop("unknown", [])

    known_roles = roles_module.list_roles()
    known_role_names = {role.name for role in known_roles}

    agents: list[dict[str, Any]] = []
    for role in known_roles:
        stats = _agent_role_stats(grouped.get(role.name, []))
        agents.append(
            {
                "name": role.name,
                "display_name": role.display_name,
                "title": role.title,
                **stats,
            }
        )
    # A role observed in the data but no longer in the current roster --
    # surfaced honestly (never silently dropped), just without identity
    # metadata the (now-gone) roles.yaml entry used to provide.
    for name in sorted(set(grouped.keys()) - known_role_names):
        stats = _agent_role_stats(grouped[name])
        agents.append({"name": name, "display_name": None, "title": None, **stats})

    agents.sort(key=lambda a: -a["cost_usd"])

    unknown_stats = _agent_role_stats(unknown_rows)
    unknown_stats.pop("attributed", None)
    # Why each row is here, not just how many there are. The single number
    # this replaces was described in the UI as legacy pre-attribution
    # history; on real data it was overwhelmingly roleless `shell` steps,
    # with a much smaller set of genuinely unattributed model invocations
    # hidden inside it.
    unknown_stats["breakdown"] = _unknown_breakdown(unknown_rows)

    return {
        "agents": agents,
        "unknown": unknown_stats,
        "note": _AGENTS_ATTRIBUTION_NOTE,
    }


# ---------------------------------------------------------------------------
# verdicts_summary / lessons_summary (Mirador Agent Panels backend sprint) —
# tenant-scoped, role-filterable reads over `verdicts`/`lessons`, plus a
# per-role aggregation, for GET /v1/verdicts and GET /v1/lessons.
# ---------------------------------------------------------------------------


def verdicts_summary(
    tenant: str | None = None,
    role: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Recent verdicts (tenant-scoped via `state_service.list_verdicts`'s
    fail-closed `LEFT JOIN runs`) plus a per-role aggregation of decision/
    kind counts, backing `GET /v1/verdicts`.
    """
    rows = state_service.list_verdicts(tenant=tenant, role=role, limit=limit)
    by_role: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("role") or "unknown"
        bucket = by_role.setdefault(
            key, {"total": 0, "decision_counts": defaultdict(int), "kind_counts": defaultdict(int)}
        )
        bucket["total"] += 1
        bucket["decision_counts"][row.get("decision") or "unknown"] += 1
        bucket["kind_counts"][row.get("kind") or "unknown"] += 1

    by_role_out = {
        key: {
            "total": bucket["total"],
            "decision_counts": dict(bucket["decision_counts"]),
            "kind_counts": dict(bucket["kind_counts"]),
        }
        for key, bucket in by_role.items()
    }
    return {"verdicts": rows, "by_role": by_role_out}


def lessons_summary(
    tenant: str | None = None,
    role: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Recent lessons (tenant-scoped via
    `state_service.list_lessons_by_tenant`'s fail-closed `LEFT JOIN runs`,
    both validated lessons AND candidates) plus a per-role aggregation
    (total / validated count / average score), backing `GET /v1/lessons`.
    """
    rows = state_service.list_lessons_by_tenant(tenant=tenant, role=role, limit=limit)
    by_role: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("role") or "unknown"
        bucket = by_role.setdefault(key, {"total": 0, "validated": 0, "use_count": 0, "scores": []})
        bucket["total"] += 1
        if row.get("validated"):
            bucket["validated"] += 1
        bucket["use_count"] += row.get("use_count") or 0
        if row.get("score") is not None:
            bucket["scores"].append(row["score"])

    by_role_out: dict[str, dict[str, Any]] = {}
    for key, bucket in by_role.items():
        scores = bucket["scores"]
        by_role_out[key] = {
            "total": bucket["total"],
            "validated": bucket["validated"],
            "use_count": bucket["use_count"],
            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
        }
    return {"lessons": rows, "by_role": by_role_out}
