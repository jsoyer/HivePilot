"""Tests for the realtime change/event bus (HP-40, Cycle 1 · P1).

The autouse `_isolate_state_db` fixture (conftest.py) gives each test an empty
per-test DB, so `change_log` starts empty every time.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from hivepilot.services import db, events, state_service


class TestEmitAndRead:
    def test_emit_appends_and_read_since_returns_it(self) -> None:
        cid = events.emit("run.started", "run", 1, payload={"status": "running"})
        assert isinstance(cid, int) and cid > 0
        rows = events.read_since(0)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "run.started"
        assert row["entity_type"] == "run"
        assert row["entity_id"] == "1"  # entity_id is stored as TEXT
        assert row["tenant"] == "default"
        assert row["payload"] == {"status": "running"}  # JSON decoded

    def test_read_since_is_a_watermark(self) -> None:
        a = events.emit("run.started", "run", 1)
        b = events.emit("step.recorded", "run", 1)
        assert events.read_since(0) and len(events.read_since(0)) == 2
        after_a = events.read_since(a)
        assert [r["id"] for r in after_a] == [b]

    def test_read_since_respects_limit_and_order(self) -> None:
        ids = [events.emit("step.recorded", "run", 1) for _ in range(5)]
        first_two = events.read_since(0, limit=2)
        assert [r["id"] for r in first_two] == ids[:2]

    def test_latest_change_id_tracks_head(self) -> None:
        assert events.latest_change_id() == 0
        last = None
        for _ in range(3):
            last = events.emit("run.started", "run", 1)
        assert events.latest_change_id() == last

    def test_oversized_payload_is_dropped_but_fact_is_kept(self) -> None:
        huge = {"blob": "x" * (events._MAX_PAYLOAD_BYTES + 100)}
        cid = events.emit("run.started", "run", 1, payload=huge)
        assert isinstance(cid, int)
        row = events.read_since(0)[0]
        assert row["payload"] is None  # body dropped, row retained

    def test_emit_is_fail_safe(self, monkeypatch) -> None:
        """A broken write must never propagate out of emit (it would break the
        run/step write that triggered it)."""

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(events.db, "connect", _boom)
        assert events.emit("run.started", "run", 1) is None


class TestPostgresNotify:
    def test_emit_issues_pg_notify_on_postgres(self, monkeypatch) -> None:
        """On Postgres, emit both appends to the log AND fires pg_notify with a
        compact envelope (id + kind + entity), so a LISTEN consumer is woken."""
        calls: list[tuple] = []

        class _FakeConn:
            def execute(self, sql, params=None):
                calls.append((sql, params))

        @contextmanager
        def _fake_connect():
            yield _FakeConn()

        monkeypatch.setattr(db, "is_postgres", lambda: True)
        monkeypatch.setattr(state_service, "init_db", lambda: None)  # isolate notify wiring
        monkeypatch.setattr(events.db, "connect", _fake_connect)
        monkeypatch.setattr(events.db, "insert_returning_id", lambda conn, sql, params: 42)

        cid = events.emit("run.completed", "run", 7, tenant="acme", payload={"status": "success"})
        assert cid == 42
        notify_calls = [c for c in calls if "pg_notify" in c[0]]
        assert len(notify_calls) == 1
        sql, params = notify_calls[0]
        assert params[0] == events.CHANNEL
        assert '"id": 42' in params[1] and '"entity_id": "7"' in params[1]


class TestLifecycleEmits:
    def test_run_start_emits(self) -> None:
        state_service.record_run_start("proj", "task", tenant="acme")
        rows = events.read_since(0)
        assert len(rows) == 1
        assert rows[0]["kind"] == "run.started"
        assert rows[0]["tenant"] == "acme"
        assert rows[0]["payload"]["project"] == "proj"

    def test_step_emits_with_run_tenant(self) -> None:
        run_id = state_service.record_run_start("proj", "task", tenant="acme")
        state_service.record_step(run_id, "plan", "success", role="ciso")
        step_events = [r for r in events.read_since(0) if r["kind"] == "step.recorded"]
        assert len(step_events) == 1
        ev = step_events[0]
        assert ev["entity_type"] == "run" and ev["entity_id"] == str(run_id)
        assert ev["tenant"] == "acme"  # inherited from the run, not defaulted
        assert ev["payload"] == {
            "run_id": run_id,
            "step": "plan",
            "status": "success",
            "role": "ciso",
        }

    def test_complete_run_emits_with_run_tenant(self) -> None:
        run_id = state_service.record_run_start("proj", "task", tenant="acme")
        state_service.complete_run(run_id, "success")
        done = [r for r in events.read_since(0) if r["kind"] == "run.completed"]
        assert len(done) == 1
        assert done[0]["tenant"] == "acme"
        assert done[0]["payload"] == {"run_id": run_id, "status": "success"}

    def test_full_lifecycle_ordering(self) -> None:
        run_id = state_service.record_run_start("proj", "task")
        state_service.record_step(run_id, "plan", "running")
        state_service.complete_run(run_id, "success")
        kinds = [r["kind"] for r in events.read_since(0)]
        assert kinds == ["run.started", "step.recorded", "run.completed"]


class TestSubscribe:
    def test_replays_from_zero_in_order(self) -> None:
        ids = [events.emit("run.started", "run", i) for i in range(3)]
        seen = []
        for row in events.subscribe(after_id=0, poll_interval=0.01, idle_timeout=0.06):
            seen.append(row["id"])
        assert seen == ids

    def test_from_now_skips_old_and_streams_new(self) -> None:
        events.emit("run.started", "run", 1)
        events.emit("run.started", "run", 2)  # both "old" relative to subscribe

        def _emit_soon():
            time.sleep(0.03)
            events.emit("run.completed", "run", 3, payload={"status": "success"})

        t = threading.Thread(target=_emit_soon)
        t.start()
        seen = []
        for row in events.subscribe(after_id=None, poll_interval=0.01, idle_timeout=0.2):
            seen.append(row)
            break  # got the one new event
        t.join()
        assert len(seen) == 1
        assert seen[0]["kind"] == "run.completed"
        assert seen[0]["payload"] == {"status": "success"}

    def test_stop_event_ends_stream(self) -> None:
        events.emit("run.started", "run", 1)
        stop = threading.Event()
        stop.set()  # already stopped -> generator yields nothing and returns
        seen = list(events.subscribe(after_id=0, poll_interval=0.01, stop=stop))
        assert seen == []
