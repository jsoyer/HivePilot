"""HP-56: per-role routines — cron next-run, replace_key dedup, daemon contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from hivepilot.services import routine_service
from hivepilot.services.routine_service import RoutineError


def test_next_fire_picks_soonest_cron_in_timezone():
    after = datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)  # Monday 06:00 UTC
    nxt = routine_service.next_fire(["0 9 * * 1", "0 18 * * 1"], "Europe/Paris", after=after)
    # 09:00 Paris on that Monday is 07:00 UTC (CEST).
    assert nxt == datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc)


def test_next_fire_rejects_bad_cron_and_timezone():
    with pytest.raises(RoutineError, match="invalid cron"):
        routine_service.next_fire(["not-a-cron"], "UTC")
    with pytest.raises(RoutineError, match="unknown timezone"):
        routine_service.next_fire(["0 9 * * *"], "Not/AZone")
    with pytest.raises(RoutineError, match="at least one"):
        routine_service.validate_crons([])


def test_unknown_role_and_missing_command_task(monkeypatch):
    with pytest.raises(RoutineError, match="unknown role"):
        routine_service.resolve_command_task("no-such-role")

    class _Bare:
        command_task = None

    monkeypatch.setattr(routine_service.roles, "get_role", lambda name: _Bare())
    with pytest.raises(RoutineError, match="no command_task"):
        routine_service.resolve_command_task("auditor")


def test_upsert_persists_next_run_and_replace_key_keeps_id():
    first = routine_service.upsert_routine(
        role="developer",
        crons=["0 9 * * 1"],
        timezone="UTC",
        projects=["example-api"],
        replace_key="dev-weekly",
    )
    assert first.role == "developer"
    assert first.replace_key == "dev-weekly"
    assert first.next_run_at is not None
    assert first.next_run_at > datetime.now(timezone.utc) - timedelta(seconds=1)

    second = routine_service.upsert_routine(
        role="developer",
        crons=["0 10 * * 1"],
        timezone="UTC",
        projects=["example-api", "example-api"],
        replace_key="dev-weekly",
    )
    assert second.id == first.id
    assert second.crons == ["0 10 * * 1"]
    assert second.projects == ["example-api"]
    assert len(routine_service.list_routines(tenant="default")) == 1


def test_due_routines_respects_enabled_and_next_run():
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    due = routine_service.upsert_routine(
        role="developer",
        crons=["*/15 * * * *"],
        timezone="UTC",
        replace_key="due-one",
    )
    later = routine_service.upsert_routine(
        role="developer",
        crons=["0 0 1 1 *"],
        timezone="UTC",
        replace_key="later-one",
    )
    disabled = routine_service.upsert_routine(
        role="developer",
        crons=["*/15 * * * *"],
        timezone="UTC",
        enabled=False,
        replace_key="off-one",
    )
    from hivepilot.services import db, state_service

    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("UPDATE routines SET next_run_at=? WHERE id=?"),
            (past.isoformat(), due.id),
        )
        conn.execute(
            db.ph("UPDATE routines SET next_run_at=? WHERE id=?"),
            (past.isoformat(), disabled.id),
        )

    found = {r.id for r in routine_service.due_routines()}
    assert due.id in found
    assert later.id not in found
    assert disabled.id not in found


def test_run_routine_dispatches_command_task_and_advances_cadence():
    routine = routine_service.upsert_routine(
        role="developer",
        crons=["0 9 * * *"],
        timezone="UTC",
        projects=["example-api"],
        replace_key="run-ok",
    )
    orch = MagicMock()
    assert routine_service.run_routine(routine, orch) is True
    orch.run_task.assert_called_once_with(
        project_names=["example-api"],
        task_name="developer",
        extra_prompt=None,
        auto_git=False,
    )
    stamped = routine_service.get_routine(routine.id)
    assert stamped is not None
    assert stamped.last_run_at is not None
    assert stamped.next_run_at is not None
    assert stamped.next_run_at > stamped.last_run_at


def test_run_routine_failure_marks_and_enqueues_retry(monkeypatch):
    routine = routine_service.upsert_routine(
        role="developer",
        crons=["0 9 * * *"],
        timezone="UTC",
        projects=["example-api"],
        replace_key="run-fail",
    )
    orch = MagicMock()
    orch.run_task.side_effect = RuntimeError("quota")
    enqueued: list[dict] = []

    def _enqueue(**kwargs):
        enqueued.append(kwargs)
        return 1

    monkeypatch.setattr("hivepilot.services.retry_service.enqueue", _enqueue)
    assert routine_service.run_routine(routine, orch) is False
    assert enqueued[0]["schedule_name"] == f"routine:{routine.id}"
    assert enqueued[0]["task"] == "developer"
    stamped = routine_service.get_routine(routine.id)
    assert stamped is not None
    assert stamped.last_run_at is not None


def test_delete_routine_and_lookup_by_replace_key():
    row = routine_service.upsert_routine(
        role="developer",
        crons=["0 9 * * *"],
        timezone="UTC",
        replace_key="to-delete",
    )
    assert routine_service.get_routine("to-delete", tenant="default") is not None
    assert routine_service.delete_routine(row.id, tenant="default") is True
    assert routine_service.get_routine(row.id, tenant="default") is None
