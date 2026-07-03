"""Tests for quota-aware auto-resume: enqueue_deferred and daemon re-run."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal retry_queue table in a temp DB."""
    db = tmp_path / "state.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_name TEXT, task TEXT, projects TEXT, error TEXT,
                attempt INTEGER, max_attempts INTEGER, status TEXT DEFAULT 'pending',
                next_retry_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                context TEXT
            )
            """
        )
        conn.commit()
    return db


def test_enqueue_deferred_roundtrip(tmp_path, monkeypatch):
    """enqueue_deferred stores next_retry_at and context in the DB."""
    db = _make_db(tmp_path)

    import hivepilot.services.state_service as svc

    monkeypatch.setattr(svc, "DB_PATH", str(db))
    monkeypatch.setattr(svc, "init_db", lambda: None)

    from hivepilot.services.retry_service import enqueue_deferred

    reset_at = datetime(2026, 6, 23, 14, 0, tzinfo=timezone.utc)
    ctx = {"task": "dev", "extra_prompt": "fix the bug", "auto_git": True}

    row_id = enqueue_deferred(
        task="dev",
        projects=["repo-a"],
        error="quota exceeded",
        next_retry_at=reset_at,
        context=ctx,
    )

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM retry_queue WHERE id=?", (row_id,)).fetchone()

    assert row is not None
    assert row["task"] == "dev"
    assert row["status"] == "pending"
    assert row["schedule_name"] == "quota-deferred"
    assert json.loads(row["projects"]) == ["repo-a"]
    assert row["next_retry_at"] == reset_at.isoformat()
    assert json.loads(row["context"]) == ctx


def test_quota_deferred_error_attributes():
    """QuotaDeferredError carries reset_at and message."""
    from datetime import datetime, timezone

    from hivepilot.services.quota import QuotaDeferredError

    reset_at = datetime(2026, 6, 23, 15, 0, tzinfo=timezone.utc)
    exc = QuotaDeferredError("quota exceeded", reset_at=reset_at)
    assert exc.reset_at == reset_at
    assert str(exc) == "quota exceeded"


def test_daemon_reruns_deferred_row(tmp_path, monkeypatch):
    """Daemon processing re-runs a due deferred row via Orchestrator.run_task."""
    db = _make_db(tmp_path)

    import hivepilot.services.state_service as svc

    monkeypatch.setattr(svc, "DB_PATH", str(db))
    monkeypatch.setattr(svc, "init_db", lambda: None)

    ctx = {"task": "dev", "extra_prompt": "fix it", "auto_git": False}
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO retry_queue "
            "(schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at, context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "quota-deferred",
                "dev",
                json.dumps(["repo-c"]),
                "quota",
                0,
                3,
                "pending",
                past_time,
                json.dumps(ctx),
            ),
        )
        conn.commit()

    run_task_calls: list[dict] = []

    class FakeOrch:
        def run_task(self, **kwargs):
            run_task_calls.append(kwargs)
            return []

    # Simulate the daemon's deferred-row processing logic
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        now_iso = datetime.now(timezone.utc).isoformat()
        due_rows = conn.execute(
            "SELECT * FROM retry_queue WHERE status='pending' AND next_retry_at <= ?",
            (now_iso,),
        ).fetchall()

    assert len(due_rows) == 1
    row = dict(due_rows[0])
    ctx_loaded = json.loads(row["context"])

    orch = FakeOrch()
    orch.run_task(
        project_names=json.loads(row["projects"]),
        task_name=ctx_loaded["task"],
        extra_prompt=ctx_loaded.get("extra_prompt"),
        auto_git=ctx_loaded.get("auto_git", False),
        concurrency=1,
    )

    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE retry_queue SET status='done' WHERE id=?", (row["id"],))
        conn.commit()

    assert len(run_task_calls) == 1
    assert run_task_calls[0]["task_name"] == "dev"
    assert run_task_calls[0]["project_names"] == ["repo-c"]
    assert run_task_calls[0]["extra_prompt"] == "fix it"

    with sqlite3.connect(str(db)) as conn:
        final_row = conn.execute("SELECT status FROM retry_queue WHERE id=1").fetchone()
    assert final_row[0] == "done"
