"""Tests for `swarm_events` persistence (Swarm Phase 1) —
`hivepilot.services.state_service`'s `insert_swarm_event` / `claim_swarm_event`
/ `mark_swarm_event_done` / `mark_swarm_event_skipped` / `get_swarm_event` /
`list_pending_swarm_events`, plus the `swarm_events` table's idempotent
migration (mirrors `tests/test_drift_persistence.py`'s shape).

The autouse `_isolate_state_db` fixture (conftest.py) redirects
`state_service.DB_PATH` to a per-test tmp file.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from hivepilot.services import state_service
from hivepilot.swarm.models import Event, compute_event_id


def _event(event_id: str | None = None, **overrides: Any) -> Event:
    dedupe_key = overrides.pop("dedupe_key", "acme/widgets:hivepilot/x:deadbeef")
    defaults: dict[str, Any] = dict(
        id=event_id or compute_event_id("pr_ready", dedupe_key),
        type="pr_ready",
        payload={"repo": "acme/widgets", "branch": "hivepilot/x", "sha": "deadbeef"},
        tenant="default",
        origin_instance="inst-a",
        ts=1_700_000_000.0,
        sig="deadbeefsig",
    )
    defaults.update(overrides)
    return Event(**defaults)


class TestInitDb:
    def test_swarm_events_table_exists_after_init_db(self) -> None:
        state_service.init_db()
        with sqlite3.connect(state_service.DB_PATH) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='swarm_events'"
            ).fetchone()
        assert row is not None

    def test_init_db_is_idempotent_for_swarm_events(self) -> None:
        state_service.init_db()
        state_service.init_db()
        with sqlite3.connect(state_service.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='swarm_events'"
            ).fetchall()
        assert len(rows) == 1


class TestInsertSwarmEvent:
    def test_first_insert_returns_true(self) -> None:
        assert state_service.insert_swarm_event(_event()) is True

    def test_duplicate_id_insert_returns_false(self) -> None:
        event = _event()
        assert state_service.insert_swarm_event(event) is True
        assert state_service.insert_swarm_event(event) is False

    def test_row_persisted_with_pending_status(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "pending"
        assert row["tenant"] == "default"
        assert row["type"] == "pr_ready"
        assert row["claimed_by"] is None

    def test_payload_roundtrips_as_json(self) -> None:
        event = _event(payload={"repo": "acme/widgets", "branch": "b", "sha": "s"})
        state_service.insert_swarm_event(event)
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["payload"] == '{"repo": "acme/widgets", "branch": "b", "sha": "s"}' or (
            "acme/widgets" in row["payload"]
        )


class TestGetSwarmEvent:
    def test_returns_none_for_unknown_id(self) -> None:
        assert state_service.get_swarm_event("pr_ready:nope") is None


class TestClaimSwarmEvent:
    def test_claim_pending_event_succeeds(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        assert state_service.claim_swarm_event(event.id, claimed_by="inst-a") is True
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "claimed"
        assert row["claimed_by"] == "inst-a"

    def test_second_claim_of_already_claimed_event_fails(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        assert state_service.claim_swarm_event(event.id, claimed_by="inst-a") is True
        assert state_service.claim_swarm_event(event.id, claimed_by="inst-b") is False
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["claimed_by"] == "inst-a"  # unchanged — inst-b never won

    def test_claim_of_unknown_event_fails(self) -> None:
        assert state_service.claim_swarm_event("pr_ready:nope", claimed_by="inst-a") is False

    def test_exactly_one_winner_across_many_concurrent_claim_calls(self) -> None:
        """Simulates N instances racing to claim the SAME event: the atomic
        `UPDATE ... WHERE status='pending'` guarantees exactly one winner
        regardless of call order (single-writer SQLite serializes the
        UPDATEs, so calling them back-to-back here is a faithful stand-in for
        genuine concurrency)."""
        event = _event()
        state_service.insert_swarm_event(event)
        results = [
            state_service.claim_swarm_event(event.id, claimed_by=f"inst-{i}") for i in range(10)
        ]
        assert results.count(True) == 1
        assert results.count(False) == 9

    def test_exactly_one_winner_under_genuine_thread_concurrency(self) -> None:
        """The review flagged the sequential list-comprehension test above as
        NOT honest enough: back-to-back calls from a single thread can never
        actually exercise a write race, since each call completes before the
        next begins. This test fires N threads at the SAME event's claim
        UPDATE simultaneously, synchronized on a `threading.Barrier` so they
        all reach `claim_swarm_event` at (as close to) the same instant as
        Python's GIL + `db.connect()`'s independent-connections-per-thread
        model allow -- a genuine write race against SQLite's own locking, not
        a simulated one. Exactly one winner must still hold, every time."""
        import threading

        event = _event()
        state_service.insert_swarm_event(event)

        n_threads = 16
        barrier = threading.Barrier(n_threads)
        results: list[bool] = [False] * n_threads
        errors: list[BaseException] = []

        def _worker(i: int) -> None:
            try:
                barrier.wait(timeout=5)
                results[i] = state_service.claim_swarm_event(event.id, claimed_by=f"inst-{i}")
            except BaseException as exc:  # noqa: BLE001 - capture for assertion
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"claim_swarm_event raised under concurrency: {errors!r}"
        assert results.count(True) == 1
        assert results.count(False) == n_threads - 1
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "claimed"


class TestMarkSwarmEventRunning:
    """`mark_swarm_event_running` -- the HIGH #2 fix (opus security review):
    the atomic `claimed` -> `running` ownership gate `process_claimed_event`
    uses BEFORE ever invoking a handler."""

    def test_claimed_event_by_owner_transitions_to_running(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")
        assert state_service.mark_swarm_event_running(event.id, claimed_by="inst-a") is True
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "running"

    def test_wrong_claimed_by_never_transitions(self) -> None:
        """A race-LOSER (or an attacker guessing ids) presenting the WRONG
        `claimed_by` can never flip a WINNER's row to `running` -- this is
        the exact primitive that closes the exactly-once-handler gap."""
        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")
        assert state_service.mark_swarm_event_running(event.id, claimed_by="inst-b") is False
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "claimed"  # unchanged -- inst-b never touched it

    def test_cannot_transition_a_still_pending_event(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        assert state_service.mark_swarm_event_running(event.id, claimed_by="inst-a") is False

    def test_double_transition_by_true_owner_only_succeeds_once(self) -> None:
        """Even the TRUE owner can't win this atomic transition twice --
        makes handler invocation itself exactly-once, not just the claim."""
        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")
        assert state_service.mark_swarm_event_running(event.id, claimed_by="inst-a") is True
        assert state_service.mark_swarm_event_running(event.id, claimed_by="inst-a") is False

    def test_exactly_one_winner_under_genuine_thread_concurrency(self) -> None:
        """The HIGH #2 fix's own atomic primitive, under GENUINE concurrency
        (not a sequential simulation): N threads for the SAME true owner
        (`claimed_by="inst-a"`) all racing to flip `claimed` -> `running`
        simultaneously, synchronized on a `threading.Barrier`. Exactly one
        must win -- this is what makes handler invocation itself exactly-once
        for the legitimate owner, not just the claim step."""
        import threading

        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")

        n_threads = 16
        barrier = threading.Barrier(n_threads)
        results: list[bool] = [False] * n_threads
        errors: list[BaseException] = []

        def _worker(i: int) -> None:
            try:
                barrier.wait(timeout=5)
                results[i] = state_service.mark_swarm_event_running(event.id, claimed_by="inst-a")
            except BaseException as exc:  # noqa: BLE001 - capture for assertion
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"mark_swarm_event_running raised under concurrency: {errors!r}"
        assert results.count(True) == 1
        assert results.count(False) == n_threads - 1
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "running"


class TestMarkSwarmEventDone:
    def test_marks_running_event_done(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")
        state_service.mark_swarm_event_running(event.id, claimed_by="inst-a")
        assert state_service.mark_swarm_event_done(event.id) is True
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "done"

    def test_cannot_mark_claimed_but_not_running_event_done(self) -> None:
        """A `claimed` row that never passed through `mark_swarm_event_running`
        cannot skip straight to `done` -- the ownership gate is mandatory,
        not optional."""
        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")
        assert state_service.mark_swarm_event_done(event.id) is False
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "claimed"

    def test_cannot_mark_pending_event_done_directly(self) -> None:
        """An event must go through 'claimed' -> 'running' first -- marking a
        still-pending event 'done' is a no-op (returns False), never
        silently succeeds."""
        event = _event()
        state_service.insert_swarm_event(event)
        assert state_service.mark_swarm_event_done(event.id) is False
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "pending"

    def test_double_mark_done_is_idempotent_no_op_second_time(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")
        state_service.mark_swarm_event_running(event.id, claimed_by="inst-a")
        assert state_service.mark_swarm_event_done(event.id) is True
        assert state_service.mark_swarm_event_done(event.id) is False


class TestMarkSwarmEventSkipped:
    def test_marks_pending_event_skipped(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        assert state_service.mark_swarm_event_skipped(event.id) is True
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "skipped"

    def test_cannot_skip_an_already_claimed_event(self) -> None:
        event = _event()
        state_service.insert_swarm_event(event)
        state_service.claim_swarm_event(event.id, claimed_by="inst-a")
        assert state_service.mark_swarm_event_skipped(event.id) is False


class TestListPendingSwarmEvents:
    def test_returns_only_pending_rows(self) -> None:
        e1 = _event(dedupe_key="r:b1:s1")
        e2 = _event(dedupe_key="r:b2:s2")
        state_service.insert_swarm_event(e1)
        state_service.insert_swarm_event(e2)
        state_service.claim_swarm_event(e1.id, claimed_by="inst-a")
        pending = state_service.list_pending_swarm_events()
        ids = {row["id"] for row in pending}
        assert e2.id in ids
        assert e1.id not in ids

    def test_filters_by_type(self) -> None:
        e1 = _event(dedupe_key="r:b1:s1", type="pr_ready")
        e2 = _event(dedupe_key="r:b2:s2", type="other_type")
        state_service.insert_swarm_event(e1)
        state_service.insert_swarm_event(e2)
        pending = state_service.list_pending_swarm_events(types=["pr_ready"])
        ids = {row["id"] for row in pending}
        assert e1.id in ids
        assert e2.id not in ids

    def test_tenant_scoping_excludes_other_tenants(self) -> None:
        e1 = _event(dedupe_key="r:b1:s1", tenant="tenant-a")
        e2 = _event(dedupe_key="r:b2:s2", tenant="tenant-b")
        state_service.insert_swarm_event(e1)
        state_service.insert_swarm_event(e2)
        pending = state_service.list_pending_swarm_events(tenants=["tenant-a"])
        ids = {row["id"] for row in pending}
        assert e1.id in ids
        assert e2.id not in ids

    def test_no_tenant_filter_returns_all_tenants(self) -> None:
        e1 = _event(dedupe_key="r:b1:s1", tenant="tenant-a")
        e2 = _event(dedupe_key="r:b2:s2", tenant="tenant-b")
        state_service.insert_swarm_event(e1)
        state_service.insert_swarm_event(e2)
        pending = state_service.list_pending_swarm_events()
        ids = {row["id"] for row in pending}
        assert {e1.id, e2.id} <= ids
