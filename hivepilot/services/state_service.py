from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import Enum
from typing import Any

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)
DB_PATH = settings.resolve_path(settings.state_db)

# ---------------------------------------------------------------------------
# Formal run-status enum
# ---------------------------------------------------------------------------

# The enum values deliberately match the historical string literals stored in
# the SQLite ``status`` column so existing rows remain fully compatible.


class RunStatus(str, Enum):
    """Canonical pipeline run-status values.

    Inherits ``str`` so that ``RunStatus.RUNNING == "running"`` is ``True``
    and values can be stored directly in the SQLite ``status`` column without
    conversion.

    Backward-compatible: the legacy strings ``'running'``, ``'pending'``, and
    ``'complete'`` are accepted via :meth:`from_str`.
    """

    # --- primary states ---
    NEW = "new"
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    REVIEW = "review"
    APPROVAL = "approval"
    COMPLETE = "complete"

    # --- failure states ---
    RATE_LIMIT = "rate_limit"
    AUTH_EXPIRED = "auth_expired"
    TEST_FAILURE = "test_failure"
    SECURITY_BLOCKER = "security_blocker"

    @classmethod
    def from_str(cls, value: str) -> "RunStatus":
        """Return the ``RunStatus`` for *value*.

        Accepts:
        - Any ``RunStatus`` member name (case-insensitive), e.g. ``"RUNNING"``
        - Any ``RunStatus`` member value, e.g. ``"running"``
        - Legacy alias ``"pending"`` -> :attr:`NEW`

        Raises
        ------
        ValueError
            If *value* cannot be mapped to any known status.
        """
        normalised = value.strip().lower()

        # Legacy alias
        if normalised == "pending":
            return cls.NEW

        # Try by value first (covers "running", "complete", ...)
        try:
            return cls(normalised)
        except ValueError:
            pass

        # Try by name (covers "RUNNING", "running" as name, ...)
        upper = normalised.upper()
        try:
            return cls[upper]
        except KeyError:
            pass

        raise ValueError(f"Unknown status: {value!r}")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT,
                task TEXT,
                status TEXT,
                detail TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                step TEXT,
                status TEXT,
                detail TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                actor TEXT,
                action TEXT,
                target TEXT,
                summary TEXT,
                metadata TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_runs (
                name TEXT PRIMARY KEY,
                last_run TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                run_id INTEGER PRIMARY KEY,
                project TEXT,
                task TEXT,
                metadata TEXT,
                status TEXT DEFAULT 'pending',
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_by TEXT,
                approved_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                role TEXT,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT, role TEXT, endpoint TEXT, method TEXT, result TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_name TEXT, task TEXT, projects TEXT, error TEXT,
                attempt INTEGER, max_attempts INTEGER, status TEXT DEFAULT 'pending',
                next_retry_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                name TEXT PRIMARY KEY,
                url TEXT,
                status TEXT,
                detail TEXT,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def upsert_worker(name: str, url: str, status: str, detail: str | None = None) -> None:
    """Record/refresh a worker's health (pull model: hub pinged its /health)."""
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO workers (name, url, status, detail, last_seen)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                url=excluded.url, status=excluded.status,
                detail=excluded.detail, last_seen=CURRENT_TIMESTAMP
            """,
            (name, url, status, detail),
        )
        conn.commit()


def list_workers() -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def record_run_start(project: str, task: str, status: str = "running") -> int:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO runs (project, task, status) VALUES (?, ?, ?)",
            (project, task, status),
        )
        conn.commit()
        run_id = cursor.lastrowid
        logger.info("state.run_start", run_id=run_id, project=project, task=task, status=status)
        return run_id


def record_step(run_id: int, step: str, status: str, detail: str | None = None) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO steps (run_id, step, status, detail) VALUES (?, ?, ?, ?)",
            (run_id, step, status, detail),
        )
        conn.commit()


def complete_run(run_id: int, status: str, detail: str | None = None) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE runs SET status=?, detail=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, detail, run_id),
        )
        conn.commit()
    logger.info("state.run_complete", run_id=run_id, status=status)


def list_recent_runs(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_steps_for_run(run_id: int) -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY timestamp", (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def record_interaction(
    actor: str,
    action: str,
    target: str | None,
    summary: str,
    timestamp: str | None = None,
    run_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO interactions (run_id, actor, action, target, summary, metadata, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                run_id,
                actor,
                action,
                target,
                summary,
                json.dumps(metadata) if metadata is not None else None,
                timestamp,
            ),
        )
        conn.commit()
        interaction_id = cursor.lastrowid
        logger.info(
            "state.interaction",
            interaction_id=interaction_id,
            actor=actor,
            action=action,
            run_id=run_id,
        )
        return interaction_id


def list_recent_interactions(limit: int = 50, run_id: int | None = None) -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if run_id is not None:
            rows = conn.execute(
                "SELECT * FROM interactions WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM interactions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


def get_schedule_last_run(name: str) -> datetime | None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT last_run FROM schedule_runs WHERE name=?", (name,)).fetchone()
    if row and row[0]:
        return datetime.fromisoformat(row[0])
    return None


def update_schedule_run(name: str) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO schedule_runs (name, last_run) VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET last_run=CURRENT_TIMESTAMP
            """,
            (name,),
        )
        conn.commit()


def record_approval_request(run_id: int, project: str, task: str, metadata: dict[str, Any]) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO approvals (run_id, project, task, metadata, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (run_id, project, task, json.dumps(metadata)),
        )
        conn.commit()


def get_pending_approvals() -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY requested_at"
        ).fetchall()
    return [dict(row) for row in rows]


def get_approval(run_id: int) -> dict[str, Any] | None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM approvals WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def update_approval(run_id: int, status: str, approver: str | None = None) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE approvals
            SET status=?, approved_by=?, approved_at=CURRENT_TIMESTAMP
            WHERE run_id=?
            """,
            (status, approver, run_id),
        )
        conn.commit()


def store_token(entry) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tokens (token, role, note) VALUES (?, ?, ?)",
            (entry.token, entry.role, entry.note),
        )
        conn.commit()


def delete_token(token: str) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM tokens WHERE token=?", (token,))
        conn.commit()


def get_token(token: str) -> dict[str, Any] | None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
    return dict(row) if row else None


def list_all_runs() -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
    return [dict(row) for row in rows]


def record_audit(
    token_hash: str,
    role: str,
    endpoint: str,
    method: str,
    result: str,
) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO audit_log (token_hash, role, endpoint, method, result) VALUES (?,?,?,?,?)",
            (token_hash, role, endpoint, method, result),
        )
        conn.commit()


def list_audit_log(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
