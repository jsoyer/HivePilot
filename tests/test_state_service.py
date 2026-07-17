"""
Tests for hivepilot.services.state_service interactions API.

The _isolate_state_db fixture (defined in conftest.py) redirects DB_PATH to
a per-test tmp file so these tests never touch the real ./state.db.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta, timezone

from hivepilot.services import db, state_service
from hivepilot.services.state_service import (
    get_schedule_last_run,
    get_steps_for_run,
    init_db,
    list_recent_interactions,
    record_interaction,
    record_run_start,
    record_step,
    update_schedule_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    actor: str = "architect",
    action: str = "reviews design",
    target: str | None = "developer",
    summary: str = "Reviewed the API design",
    run_id: int | None = None,
    metadata: dict | None = None,
    timestamp: str | None = None,
) -> int:
    return record_interaction(
        actor=actor,
        action=action,
        target=target,
        summary=summary,
        run_id=run_id,
        metadata=metadata,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# init_db — interactions table existence
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_interactions_table_exists_after_init_db(self) -> None:
        init_db()
        with sqlite3.connect(state_service.DB_PATH) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='interactions'"
            ).fetchone()
        assert row is not None, "interactions table must be created by init_db()"


# ---------------------------------------------------------------------------
# record_interaction — basic insertion
# ---------------------------------------------------------------------------


class TestRecordInteraction:
    def test_returns_integer_id(self) -> None:
        iid = _record()
        assert isinstance(iid, int)
        assert iid >= 1

    def test_ids_are_increasing(self) -> None:
        id1 = _record(actor="a", action="act1", summary="s1")
        id2 = _record(actor="b", action="act2", summary="s2")
        assert id2 > id1

    def test_stored_row_has_correct_fields(self) -> None:
        _record(
            actor="pm", action="assigns task", target="engineer", summary="Work started", run_id=7
        )
        rows = list_recent_interactions()
        assert len(rows) == 1
        row = rows[0]
        assert row["actor"] == "pm"
        assert row["action"] == "assigns task"
        assert row["target"] == "engineer"
        assert row["summary"] == "Work started"
        assert row["run_id"] == 7

    def test_row_has_all_expected_keys(self) -> None:
        _record()
        rows = list_recent_interactions()
        assert len(rows) == 1
        row = rows[0]
        for key in (
            "id",
            "actor",
            "action",
            "target",
            "summary",
            "run_id",
            "metadata",
            "timestamp",
        ):
            assert key in row, f"Expected key '{key}' in row"

    def test_none_target_stored_as_none(self) -> None:
        _record(target=None)
        rows = list_recent_interactions()
        assert rows[0]["target"] is None

    def test_none_run_id_stored_as_none(self) -> None:
        _record(run_id=None)
        rows = list_recent_interactions()
        assert rows[0]["run_id"] is None


# ---------------------------------------------------------------------------
# metadata round-trip
# ---------------------------------------------------------------------------


class TestMetadataRoundtrip:
    def test_metadata_dict_stored_as_json_string(self) -> None:
        meta = {"key": "value", "count": 3}
        _record(metadata=meta)
        rows = list_recent_interactions()
        raw = rows[0]["metadata"]
        assert isinstance(raw, str), "metadata must be stored as a JSON string"
        assert json.loads(raw) == meta

    def test_none_metadata_stays_none(self) -> None:
        _record(metadata=None)
        rows = list_recent_interactions()
        assert rows[0]["metadata"] is None

    def test_empty_metadata_dict(self) -> None:
        _record(metadata={})
        rows = list_recent_interactions()
        raw = rows[0]["metadata"]
        assert json.loads(raw) == {}


# ---------------------------------------------------------------------------
# list_recent_interactions — ordering and filtering
# ---------------------------------------------------------------------------


class TestListRecentInteractions:
    def test_returns_most_recent_first(self) -> None:
        id1 = _record(actor="first", action="a1", summary="s1")
        id2 = _record(actor="second", action="a2", summary="s2")
        rows = list_recent_interactions()
        assert rows[0]["id"] == id2
        assert rows[1]["id"] == id1

    def test_limit_caps_results(self) -> None:
        for i in range(5):
            _record(actor=f"actor{i}", action="act", summary="s")
        rows = list_recent_interactions(limit=3)
        assert len(rows) == 3

    def test_default_limit_is_50(self) -> None:
        for i in range(60):
            _record(actor=f"actor{i}", action="act", summary="s")
        rows = list_recent_interactions()
        assert len(rows) == 50

    def test_empty_when_no_interactions(self) -> None:
        rows = list_recent_interactions()
        assert rows == []

    def test_filter_by_run_id(self) -> None:
        _record(actor="a", action="act", summary="s", run_id=1)
        _record(actor="b", action="act", summary="s", run_id=2)
        _record(actor="c", action="act", summary="s", run_id=1)
        rows = list_recent_interactions(run_id=1)
        assert len(rows) == 2
        assert all(r["run_id"] == 1 for r in rows)

    def test_filter_by_run_id_returns_only_matching(self) -> None:
        _record(actor="x", action="act", summary="s", run_id=99)
        _record(actor="y", action="act", summary="s", run_id=100)
        rows = list_recent_interactions(run_id=100)
        assert len(rows) == 1
        assert rows[0]["actor"] == "y"

    def test_filter_run_id_with_limit(self) -> None:
        for i in range(5):
            _record(actor=f"a{i}", action="act", summary="s", run_id=7)
        rows = list_recent_interactions(limit=3, run_id=7)
        assert len(rows) == 3
        assert all(r["run_id"] == 7 for r in rows)


# ---------------------------------------------------------------------------
# Phase 24b.1 — steps.provider / steps.model (idempotent migration +
# record_step persistence)
# ---------------------------------------------------------------------------


class TestStepsProviderModelMigration:
    def test_columns_exist_after_init_db(self) -> None:
        init_db()
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "provider")
            assert db.column_exists(conn, "steps", "model")

    def test_init_db_is_idempotent(self) -> None:
        """Calling init_db() twice must not raise (ALTER TABLE ADD COLUMN
        guarded by column_exists, same pattern as the 'tenant' migration)."""
        init_db()
        init_db()  # must not raise "duplicate column name"
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "provider")
            assert db.column_exists(conn, "steps", "model")

    def test_pre_existing_db_without_columns_gets_them(self) -> None:
        """Simulates a pre-24b.1 DB: create the steps table WITHOUT the new
        columns directly, then call init_db() and confirm the columns are
        added without error and without touching existing rows."""
        state_service.init_db()  # creates the full up-to-date schema once

        # Drop and recreate `steps` in the OLD (pre-migration) shape to
        # simulate an existing DB predating this sprint.
        with db.connect() as conn:
            conn.execute("DROP TABLE steps")
            conn.execute(
                """
                CREATE TABLE steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    step TEXT,
                    status TEXT,
                    detail TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO steps (run_id, step, status, detail) VALUES (?, ?, ?, ?)",
                (1, "legacy-step", "success", None),
            )

        with db.connect() as conn:
            assert not db.column_exists(conn, "steps", "provider")

        init_db()  # idempotent migration must backfill the missing columns

        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "provider")
            assert db.column_exists(conn, "steps", "model")
            row = conn.execute("SELECT * FROM steps WHERE step='legacy-step'").fetchone()
        assert row is not None
        assert row["provider"] is None  # existing row untouched -> NULL, not invented


class TestRecordStepProviderModel:
    def test_persists_provider_and_model_when_given(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        rows = get_steps_for_run(run_id)
        assert len(rows) == 1
        assert rows[0]["provider"] == "claude"
        assert rows[0]["model"] == "claude-sonnet-4-6"

    def test_provider_and_model_null_when_omitted(self) -> None:
        """Backward-compat: old-style calls (no provider/model kwargs) still
        work and persist NULL — never an invented value."""
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "success")
        rows = get_steps_for_run(run_id)
        assert rows[0]["provider"] is None
        assert rows[0]["model"] is None

    def test_positional_detail_still_works_backward_compat(self) -> None:
        """Existing callers passing `detail` positionally (no provider/model)
        must be unaffected by the new keyword-only-by-convention params."""
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "failed", "boom")
        rows = get_steps_for_run(run_id)
        assert rows[0]["detail"] == "boom"
        assert rows[0]["provider"] is None
        assert rows[0]["model"] is None

    def test_provider_only_no_model(self) -> None:
        """A shell step: provider known (runner kind), model genuinely
        unknown -> NULL, not invented."""
        run_id = record_run_start("proj", "task")
        record_step(run_id, "shell-step", "success", provider="shell", model=None)
        rows = get_steps_for_run(run_id)
        assert rows[0]["provider"] == "shell"
        assert rows[0]["model"] is None


# ---------------------------------------------------------------------------
# Phase 24b.2a — steps.input_tokens / steps.output_tokens / steps.cost_usd
# (idempotent migration + record_step persistence)
# ---------------------------------------------------------------------------


class TestStepsUsageMigration:
    def test_columns_exist_after_init_db(self) -> None:
        init_db()
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "input_tokens")
            assert db.column_exists(conn, "steps", "output_tokens")
            assert db.column_exists(conn, "steps", "cost_usd")

    def test_init_db_is_idempotent(self) -> None:
        """Calling init_db() twice must not raise (ALTER TABLE ADD COLUMN
        guarded by column_exists, same pattern as provider/model)."""
        init_db()
        init_db()  # must not raise "duplicate column name"
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "input_tokens")
            assert db.column_exists(conn, "steps", "output_tokens")
            assert db.column_exists(conn, "steps", "cost_usd")

    def test_pre_existing_db_without_columns_gets_them(self) -> None:
        """Simulates a pre-24b.2a DB (has provider/model but not the usage
        columns): recreate steps in that shape, then confirm init_db()
        backfills the 3 new columns without touching existing rows."""
        state_service.init_db()  # creates the full up-to-date schema once

        with db.connect() as conn:
            conn.execute("DROP TABLE steps")
            conn.execute(
                """
                CREATE TABLE steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    step TEXT,
                    status TEXT,
                    detail TEXT,
                    provider TEXT,
                    model TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO steps (run_id, step, status, detail, provider, model) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (1, "legacy-step", "success", None, "claude", "claude-sonnet-4-6"),
            )

        with db.connect() as conn:
            assert not db.column_exists(conn, "steps", "input_tokens")

        init_db()  # idempotent migration must backfill the missing columns

        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "input_tokens")
            assert db.column_exists(conn, "steps", "output_tokens")
            assert db.column_exists(conn, "steps", "cost_usd")
            row = conn.execute("SELECT * FROM steps WHERE step='legacy-step'").fetchone()
        assert row is not None
        assert row["provider"] == "claude"  # existing row untouched
        assert row["input_tokens"] is None  # backfilled -> NULL, not invented
        assert row["output_tokens"] is None
        assert row["cost_usd"] is None


class TestRecordStepUsage:
    def test_persists_tokens_and_cost_when_given(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(
            run_id,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=123,
            output_tokens=45,
            cost_usd=0.0067,
        )
        rows = get_steps_for_run(run_id)
        assert rows[0]["input_tokens"] == 123
        assert rows[0]["output_tokens"] == 45
        assert rows[0]["cost_usd"] == 0.0067

    def test_tokens_and_cost_null_when_omitted(self) -> None:
        """Backward-compat: existing callers that never pass usage kwargs
        still work and persist NULL — never an invented value."""
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        rows = get_steps_for_run(run_id)
        assert rows[0]["input_tokens"] is None
        assert rows[0]["output_tokens"] is None
        assert rows[0]["cost_usd"] is None

    def test_fully_backward_compat_call_with_no_new_kwargs_at_all(self) -> None:
        """A caller using only the pre-24b.1 signature (no provider/model/
        usage) must still work unchanged."""
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "failed", "boom")
        rows = get_steps_for_run(run_id)
        assert rows[0]["detail"] == "boom"
        assert rows[0]["input_tokens"] is None
        assert rows[0]["output_tokens"] is None
        assert rows[0]["cost_usd"] is None


# ---------------------------------------------------------------------------
# Phase 20 D3 review — get_schedule_last_run must return a tz-aware UTC
# datetime (was returning a NAIVE datetime parsed from SQLite's
# CURRENT_TIMESTAMP, which raised TypeError when compared/subtracted against
# an aware `datetime.now(timezone.utc)` in schedule_service.due_schedules()
# and drift_schedule.due_drift_projects() -- see D3 review VERIFY 3).
# ---------------------------------------------------------------------------


class TestGetScheduleLastRunTzAware:
    def test_returns_none_when_never_run(self) -> None:
        assert get_schedule_last_run("never-run-schedule") is None

    def test_returns_tz_aware_utc_datetime_after_a_stamp(self) -> None:
        update_schedule_run("demo-schedule")
        last_run = get_schedule_last_run("demo-schedule")
        assert last_run is not None
        assert last_run.tzinfo is not None
        assert last_run.utcoffset() == timedelta(0)
        assert last_run.tzinfo == timezone.utc or last_run.utcoffset() == timezone.utc.utcoffset(
            None
        )

    def test_comparable_against_aware_now_without_raising(self) -> None:
        """Regression: the exact comparison shape used by
        schedule_service.due_schedules() and drift_schedule.due_drift_projects()
        must not raise TypeError."""
        from datetime import datetime

        update_schedule_run("demo-schedule-2")
        last_run = get_schedule_last_run("demo-schedule-2")
        assert last_run is not None
        next_run_time = last_run + timedelta(minutes=60)
        now = datetime.now(timezone.utc)
        # Must not raise "can't compare offset-naive and offset-aware datetimes"
        assert (next_run_time <= now) is False
