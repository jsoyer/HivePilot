from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)
DB_PATH = settings.resolve_path(settings.state_db)


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
        conn.commit()


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
