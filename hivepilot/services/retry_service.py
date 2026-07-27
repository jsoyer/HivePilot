from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from hivepilot.services import db, state_service

try:
    from hivepilot.services import metrics as _metrics  # noqa: F401

    _METRICS_AVAILABLE = True
except ImportError:
    _metrics = None  # type: ignore[assignment]
    _METRICS_AVAILABLE = False


def enqueue(
    *,
    schedule_name: str,
    task: str,
    projects: Iterable[str],
    error: str,
    attempt: int,
    max_attempts: int,
    base_delay_minutes: int,
) -> int:
    """Add a failed task to the retry queue and return its row id.

    The next-retry timestamp is computed with exponential backoff:
    ``delay = base_delay_minutes * 2^(attempt - 1)``  (attempt is 1-based).
    """
    state_service.init_db()
    delay = base_delay_minutes * (2 ** max(attempt - 1, 0))
    next_retry_at = (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat()
    with db.connect() as conn:
        return db.insert_returning_id(
            conn,
            "INSERT INTO retry_queue "
            "(schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?)",
            (
                schedule_name,
                task,
                json.dumps(list(projects)),
                error,
                attempt,
                max_attempts,
                next_retry_at,
            ),
        )


def enqueue_deferred(
    *,
    task: str,
    projects: list[str],
    error: str,
    next_retry_at: datetime,
    context: dict,
) -> int:
    """Insert a quota-deferred row with an explicit next_retry_at and context JSON.

    Unlike ``enqueue`` (which uses exponential backoff), this is for quota-aware
    deferral — the retry time is the quota reset window, not a backoff formula.
    Returns the inserted row id.
    """
    state_service.init_db()
    with db.connect() as conn:
        row_id = db.insert_returning_id(
            conn,
            "INSERT INTO retry_queue "
            "(schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at, context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "quota-deferred",
                task,
                json.dumps(list(projects)),
                error,
                0,
                3,
                "pending",
                next_retry_at.isoformat(),
                json.dumps(context),
            ),
        )
    if _METRICS_AVAILABLE and _metrics is not None:
        try:
            _metrics.deferred_total.inc()
        except Exception:  # noqa: BLE001
            pass
    return row_id


def list_queue(status: str | None = None) -> list[dict[str, Any]]:
    """Return retry-queue rows, optionally filtered by *status*."""
    state_service.init_db()
    with db.connect() as conn:
        if status:
            rows = conn.execute(
                db.ph("SELECT * FROM retry_queue WHERE status=? ORDER BY id"), (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM retry_queue ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def list_dlq() -> list[dict[str, Any]]:
    """Return all rows in the dead-letter queue (status='dead')."""
    return list_queue("dead")


def purge_dlq() -> int:
    """Delete all dead-letter-queue rows and return the count deleted."""
    state_service.init_db()
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM retry_queue WHERE status='dead'")
        return int(cur.rowcount)


# ---------------------------------------------------------------------------
# fix/retry-queue-drain
#
# Root cause: `enqueue()` above (the plain exponential-backoff path used by
# `schedule_service.run_entry()`'s except branch on an ordinary schedule
# task failure) writes rows with `context IS NULL`. The scheduler daemon's
# ONLY reader of this table (`scheduler_daemon._process_deferred_rows`)
# filters `WHERE ... AND context IS NOT NULL` -- a guard written exclusively
# for `enqueue_deferred()`'s quota-deferred subtype. A context-less row was
# never drained by ANYTHING: 197 `groomer-scan` retries sat `pending` and
# past-due for 7 days with zero operator-visible signal (`hivepilot schedule
# health` printed the raw count but never flagged it as abnormal).
#
# `due_backoff_rows`/`claim_row`/`mark_done`/`mark_retry`/`mark_dead`/
# `expire_stale` below give `scheduler_daemon._process_backoff_retries` (the
# new reader for this subtype) everything it needs, bounded to at most one
# row per tick -- the same "one per tick" bound `autopilot_queue.drain_one`
# uses for the identical "never fire a whole backlog at once" reason.
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse a stored `retry_queue` timestamp (`next_retry_at` or
    `created_at`) into an aware UTC `datetime`, tolerating the two formats
    genuinely written to this table: SQLite's naive `CURRENT_TIMESTAMP`
    ('YYYY-MM-DD HH:MM:SS', assumed UTC) and `enqueue()`'s own aware
    `.isoformat()` output. Returns `None` if *value* is empty/unparseable --
    callers MUST treat that as "due-ness/age unknown", never as "not due" /
    "not expired" by silently defaulting either way without a caller-visible
    consequence (see `due_backoff_rows`, `expire_stale`,
    `config_doctor.check_retry_queue_backlog`).

    Reuses `hivepilot.utils.display_time`'s own normalizer rather than
    reimplementing naive-vs-aware handling a second time -- a second,
    subtly different implementation of "is this naive or aware" is exactly
    how this codebase has shipped naive-datetime bugs before.
    """
    from hivepilot.utils.display_time import _parse_stored

    if value in (None, ""):
        return None
    return _parse_stored(value)


def due_backoff_rows(limit: int = 1, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return up to *limit* PENDING, context-less rows whose `next_retry_at`
    is due, oldest-overdue first.

    Deliberately scoped to `context IS NULL` rows only -- rows WITH a
    context blob are the quota-deferred subtype
    `scheduler_daemon._process_deferred_rows` already drains; duplicating
    that dispatch here would double-run them.

    Due-ness is computed in PYTHON via `_parse_ts` (never a raw SQL `<=`
    comparison against the TEXT column) because this table can hold both a
    naive and an aware timestamp shape -- a string comparison across two
    differently-shaped values is not guaranteed correct. A row whose
    `next_retry_at` cannot be parsed is never treated as due (fail-closed on
    dispatch); `config_doctor.check_retry_queue_backlog` independently flags
    any such row so this never goes silent.
    """
    if limit < 1:
        return []
    now = now or datetime.now(timezone.utc)
    state_service.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph("SELECT * FROM retry_queue WHERE status='pending' AND context IS NULL")
        ).fetchall()

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        row_dict = dict(row)
        parsed = _parse_ts(row_dict.get("next_retry_at"))
        if parsed is None or parsed > now:
            continue
        candidates.append((parsed, row_dict))
    candidates.sort(key=lambda pair: pair[0])
    return [row for _parsed, row in candidates[:limit]]


def claim_row(row_id: int) -> bool:
    """Atomically flip *row_id* from `pending` to `running`. Returns `True`
    iff this call won the claim -- defense-in-depth against double-dispatch,
    mirroring `autopilot_queue._claim_running`'s contract."""
    state_service.init_db()
    with db.connect() as conn:
        cursor = conn.execute(
            db.ph("UPDATE retry_queue SET status='running' WHERE id=? AND status='pending'"),
            (row_id,),
        )
        return cursor.rowcount == 1


def mark_done(row_id: int) -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(db.ph("UPDATE retry_queue SET status='done' WHERE id=?"), (row_id,))


def mark_retry(row_id: int, *, attempt: int, next_retry_at: datetime) -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("UPDATE retry_queue SET status='pending', attempt=?, next_retry_at=? WHERE id=?"),
            (attempt, next_retry_at.isoformat(), row_id),
        )


def mark_dead(row_id: int, *, attempt: int) -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("UPDATE retry_queue SET status='dead', attempt=? WHERE id=?"),
            (attempt, row_id),
        )


def expire_stale(ttl_days: float, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Mark PENDING rows whose `created_at` is older than *ttl_days* as
    `status='expired'` and return the list of rows that were expired (never
    just a count -- callers log/print each one so an operator can see WHICH
    stale context finally got dropped, and why).

    `expired` is a status distinct from `dead`: `dead` means max_attempts
    was exhausted after real dispatch attempts; `expired` means the row was
    never even re-dispatched because its context is judged too old to be
    worth trying -- e.g. a path that no longer exists after several days is
    not going to fix itself.

    A row whose `created_at` cannot be parsed is left untouched (never
    expired on unknown age) -- `config_doctor.check_retry_queue_backlog`
    independently flags any unparseable timestamp, so this never goes
    silent either. `ttl_days <= 0` disables expiry entirely (opt-out via
    `HIVEPILOT_RETRY_QUEUE_TTL_DAYS=0`). Only ever touches `status='pending'`
    rows -- `running`/`done`/`dead` rows are never revisited.
    """
    if ttl_days <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    state_service.init_db()
    with db.connect() as conn:
        rows = conn.execute(db.ph("SELECT * FROM retry_queue WHERE status='pending'")).fetchall()

    expired: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(row)
        created = _parse_ts(row_dict.get("created_at"))
        if created is None or created > cutoff:
            continue
        expired.append(row_dict)

    if expired:
        with db.connect() as conn:
            for row_dict in expired:
                conn.execute(
                    db.ph("UPDATE retry_queue SET status='expired' WHERE id=?"),
                    (row_dict["id"],),
                )
    return expired
