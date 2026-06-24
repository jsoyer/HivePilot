"""
Tests for hivepilot.services.retry_service.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import hivepilot.services.retry_service as retry_service
import hivepilot.services.state_service as state_service


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect DB_PATH to a temp file for every test."""
    db = tmp_path / "test_retry.db"
    monkeypatch.setattr(state_service, "DB_PATH", db)
    return db


class TestEnqueue:
    """Tests for retry_service.enqueue()."""

    def test_enqueue_returns_integer_id(self) -> None:
        """enqueue() returns a positive integer row id."""
        job_id = retry_service.enqueue(
            schedule_name="nightly",
            task="run_tests",
            projects=["proj-a", "proj-b"],
            error="Connection timeout",
            attempt=1,
            max_attempts=3,
            base_delay_minutes=2,
        )
        assert isinstance(job_id, int)
        assert job_id >= 1

    def test_enqueue_row_has_correct_fields(self) -> None:
        """Enqueued job has correct schedule_name, task, attempt, max_attempts, status."""
        retry_service.enqueue(
            schedule_name="daily-sync",
            task="sync_data",
            projects=["proj-x"],
            error="Timeout",
            attempt=1,
            max_attempts=5,
            base_delay_minutes=10,
        )
        rows = retry_service.list_queue()
        assert len(rows) == 1
        row = rows[0]
        assert row["schedule_name"] == "daily-sync"
        assert row["task"] == "sync_data"
        assert row["attempt"] == 1
        assert row["max_attempts"] == 5
        assert row["status"] == "pending"
        assert row["error"] == "Timeout"

    def test_enqueue_projects_json_encoded(self) -> None:
        """Projects field is stored as a JSON string."""
        retry_service.enqueue(
            schedule_name="s1",
            task="t1",
            projects=["a", "b", "c"],
            error="err",
            attempt=1,
            max_attempts=3,
            base_delay_minutes=1,
        )
        rows = retry_service.list_queue()
        assert json.loads(rows[0]["projects"]) == ["a", "b", "c"]

    def test_enqueue_exponential_backoff_attempt1(self) -> None:
        """Attempt 1: next_retry_at = now + base_delay_minutes * 2^0 = now + base_delay_minutes."""
        before = datetime.now(timezone.utc)
        retry_service.enqueue(
            schedule_name="s",
            task="t",
            projects=[],
            error="e",
            attempt=1,
            max_attempts=3,
            base_delay_minutes=10,
        )
        after = datetime.now(timezone.utc)
        rows = retry_service.list_queue()
        next_retry = datetime.fromisoformat(rows[0]["next_retry_at"])
        if next_retry.tzinfo is None:
            from datetime import timezone as tz

            next_retry = next_retry.replace(tzinfo=tz.utc)
        # Should be roughly now + 10 minutes (2^0 * 10)
        from datetime import timedelta

        assert next_retry >= before + timedelta(minutes=9, seconds=55)
        assert next_retry <= after + timedelta(minutes=10, seconds=5)

    def test_enqueue_exponential_backoff_attempt2(self) -> None:
        """Attempt 2: delay = base_delay_minutes * 2^1."""
        from datetime import timedelta

        before = datetime.now(timezone.utc)
        retry_service.enqueue(
            schedule_name="s",
            task="t",
            projects=[],
            error="e",
            attempt=2,
            max_attempts=3,
            base_delay_minutes=5,
        )
        after = datetime.now(timezone.utc)
        rows = retry_service.list_queue()
        next_retry = datetime.fromisoformat(rows[0]["next_retry_at"])
        if next_retry.tzinfo is None:
            next_retry = next_retry.replace(tzinfo=timezone.utc)
        # Should be ~now + 10 minutes (5 * 2^1)
        assert next_retry >= before + timedelta(minutes=9, seconds=55)
        assert next_retry <= after + timedelta(minutes=10, seconds=5)

    def test_enqueue_empty_projects_list(self) -> None:
        """enqueue() works with an empty projects list."""
        job_id = retry_service.enqueue(
            schedule_name="s",
            task="t",
            projects=[],
            error="e",
            attempt=1,
            max_attempts=1,
            base_delay_minutes=1,
        )
        assert isinstance(job_id, int)
        rows = retry_service.list_queue()
        assert json.loads(rows[0]["projects"]) == []


class TestListQueue:
    """Tests for retry_service.list_queue()."""

    def test_list_queue_no_filter_returns_all(self) -> None:
        """list_queue() with no status returns all jobs."""
        retry_service.enqueue(
            schedule_name="s1",
            task="t",
            projects=[],
            error="e",
            attempt=1,
            max_attempts=3,
            base_delay_minutes=1,
        )
        retry_service.enqueue(
            schedule_name="s2",
            task="t",
            projects=[],
            error="e",
            attempt=1,
            max_attempts=3,
            base_delay_minutes=1,
        )
        rows = retry_service.list_queue()
        assert len(rows) == 2

    def test_list_queue_filters_by_status(self, isolated_db: Path) -> None:
        """list_queue('pending') returns only pending jobs."""
        retry_service.enqueue(
            schedule_name="s1",
            task="t",
            projects=[],
            error="e",
            attempt=1,
            max_attempts=3,
            base_delay_minutes=1,
        )
        # Directly insert a 'running' row
        with sqlite3.connect(isolated_db) as conn:
            conn.execute(
                "INSERT INTO retry_queue (schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("s2", "t2", "[]", "e", 1, 3, "running", "2099-01-01T00:00:00"),
            )
            conn.commit()
        pending = retry_service.list_queue("pending")
        running = retry_service.list_queue("running")
        assert len(pending) == 1
        assert pending[0]["schedule_name"] == "s1"
        assert len(running) == 1
        assert running[0]["schedule_name"] == "s2"

    def test_list_queue_returns_dicts(self) -> None:
        """list_queue() returns a list of dicts."""
        retry_service.enqueue(
            schedule_name="s",
            task="t",
            projects=[],
            error="e",
            attempt=1,
            max_attempts=1,
            base_delay_minutes=1,
        )
        rows = retry_service.list_queue()
        assert isinstance(rows[0], dict)
        assert "id" in rows[0]
        assert "schedule_name" in rows[0]
        assert "next_retry_at" in rows[0]
        assert "created_at" in rows[0]


class TestListDlq:
    """Tests for retry_service.list_dlq()."""

    def test_list_dlq_returns_only_dead_jobs(self, isolated_db: Path) -> None:
        """list_dlq() returns only jobs with status='dead'."""
        # Insert pending and dead
        with sqlite3.connect(isolated_db) as conn:
            state_service.init_db()
            conn.execute(
                "INSERT INTO retry_queue (schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("s1", "t1", "[]", "e", 1, 3, "pending", "2099-01-01T00:00:00"),
            )
            conn.execute(
                "INSERT INTO retry_queue (schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("s2", "t2", "[]", "e", 3, 3, "dead", "2099-01-01T00:00:00"),
            )
            conn.commit()
        dlq = retry_service.list_dlq()
        assert len(dlq) == 1
        assert dlq[0]["status"] == "dead"
        assert dlq[0]["schedule_name"] == "s2"

    def test_list_dlq_empty(self) -> None:
        """list_dlq() returns [] when no dead jobs exist."""
        retry_service.enqueue(
            schedule_name="s",
            task="t",
            projects=[],
            error="e",
            attempt=1,
            max_attempts=3,
            base_delay_minutes=1,
        )
        assert retry_service.list_dlq() == []


class TestPurgeDlq:
    """Tests for retry_service.purge_dlq()."""

    def test_purge_dlq_deletes_dead_rows(self, isolated_db: Path) -> None:
        """purge_dlq() deletes all dead rows and returns count."""
        state_service.init_db()
        with sqlite3.connect(isolated_db) as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO retry_queue (schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"s{i}", "t", "[]", "e", 1, 3, "dead", "2099-01-01T00:00:00"),
                )
            conn.commit()
        count = retry_service.purge_dlq()
        assert count == 3
        assert retry_service.list_dlq() == []

    def test_purge_dlq_does_not_delete_pending(self, isolated_db: Path) -> None:
        """purge_dlq() leaves non-dead rows intact."""
        state_service.init_db()
        with sqlite3.connect(isolated_db) as conn:
            conn.execute(
                "INSERT INTO retry_queue (schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("s1", "t", "[]", "e", 1, 3, "dead", "2099-01-01T00:00:00"),
            )
            conn.execute(
                "INSERT INTO retry_queue (schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("s2", "t", "[]", "e", 1, 3, "pending", "2099-01-01T00:00:00"),
            )
            conn.commit()
        count = retry_service.purge_dlq()
        assert count == 1
        remaining = retry_service.list_queue()
        assert len(remaining) == 1
        assert remaining[0]["status"] == "pending"

    def test_purge_dlq_returns_zero_when_empty(self) -> None:
        """purge_dlq() returns 0 when there are no dead rows."""
        count = retry_service.purge_dlq()
        assert count == 0
