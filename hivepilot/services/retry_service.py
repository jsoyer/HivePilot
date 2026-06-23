from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from hivepilot.services import state_service


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
    with sqlite3.connect(state_service.DB_PATH) as conn:
        cur = conn.execute(
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
        conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]


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
    with sqlite3.connect(state_service.DB_PATH) as conn:
        cur = conn.execute(
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
        conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]


def list_queue(status: str | None = None) -> list[dict[str, Any]]:
    """Return retry-queue rows, optionally filtered by *status*."""
    state_service.init_db()
    with sqlite3.connect(state_service.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if status:
            rows = conn.execute(
                "SELECT * FROM retry_queue WHERE status=? ORDER BY id", (status,)
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
    with sqlite3.connect(state_service.DB_PATH) as conn:
        cur = conn.execute("DELETE FROM retry_queue WHERE status='dead'")
        conn.commit()
        return int(cur.rowcount)
