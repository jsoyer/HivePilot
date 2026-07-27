"""Tests for the startup reconciler (propose -> ratify -> dispatch PRD,
Sprint 3, spec section 8).

The property under test is the one that makes "claim BEFORE create" worth
doing at all: a crash between the atomic claim and the run-row creation
leaves a visible ``status='claimed' AND run_id IS NULL`` row, which the
reconciler rewinds to ``pending`` **exactly once** -- and which, crucially,
can never turn into a double dispatch, because a claim that DID reach
``mark_task_running`` has a ``run_id`` and is therefore never rewound.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from hivepilot.services import autopilot_queue, db, partition_service, state_service

PROJECTS_YAML = """
projects:
  acme-api:
    path: /tmp/acme-api
"""

PIPELINES_YAML = """
pipelines:
  bugfix:
    description: fix a bug
    stages:
      - name: fix
        task: implement
"""

TASKS_YAML = """
tasks:
  implement:
    description: implement something
    role: developer
"""

POLICIES_YAML = """
policies:
  default:
    require_approval: true
  projects:
    acme-api:
      outward_actions: []
      budget_daily_usd: 100.0
      max_partition_cost_usd: 50.0
      max_task_wall_clock_seconds: 3600
"""


@pytest.fixture
def live_config(tmp_path, monkeypatch):
    from hivepilot.config import settings
    from hivepilot.services import policy_service

    (tmp_path / "projects.yaml").write_text(PROJECTS_YAML, encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text(PIPELINES_YAML, encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    (tmp_path / "policies.yaml").write_text(POLICIES_YAML, encoding="utf-8")
    monkeypatch.setattr(settings, "config_repo", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "claude_max_concurrency", 8, raising=False)
    monkeypatch.setattr(settings, "concurrency_limit", 8, raising=False)
    policy_service.reload_policies()
    yield tmp_path
    policy_service.reload_policies()


@pytest.fixture(autouse=True)
def _zero_spend(monkeypatch):
    monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *, tenant="default": 0.0)


def _task(task_id: str, **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "project": "acme-api",
        "pipeline": "bugfix",
        "prompt": f"do the {task_id} work",
        "depends_on": [],
        "budget": {"wall_clock_seconds": 30, "cost_usd": 1.0},
        "done_when": ["a repro test passes"],
        "outward": False,
    }
    task.update(overrides)
    return task


def _plan(*tasks: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "partition_version": 1,
        "source": {"kind": "text", "ref": "docs/bug-1234.md", "digest": "sha256:aaa"},
        "proposer": {"role": "partitioner", "pipeline": "propose-partition", "run_id": 4711},
        "policy": {"max_parallel": 3, "on_task_failure": "continue"},
        "tasks": list(tasks) or [_task("t1")],
    }
    plan.update(overrides)
    return plan


def _ratified(plan: dict[str, Any], *, tenant: str = "default") -> str:
    plan_json = json.dumps(plan)
    partition_id = partition_service.create_partition(plan_json=plan_json, tenant=tenant)
    row = partition_service.get_partition(partition_id, tenant=tenant)
    assert row is not None
    partition_service.ratify_partition(
        partition_id,
        partition_json=plan_json,
        outward_consent=False,
        approver="operator",
        expected_digest=str(row["proposed_digest"]),
        tenant=tenant,
    )
    return partition_id


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_pipeline(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return [SimpleNamespace(project="acme-api", target="bugfix", success=True)]


class _Crash(BaseException):
    """A process-death stand-in.

    Deliberately a `BaseException`, not an `Exception`: the dispatcher's own
    `except Exception` around run-row creation is a graceful, immediate
    rewind, whereas a CRASH is precisely the case where NO handler runs and
    the `claimed` row is left behind for the reconciler. Using a plain
    `Exception` here would test the handler, not the crash.
    """


def _statuses(partition_id: str) -> dict[str, str]:
    return {
        str(row["task_id"]): str(row["status"])
        for row in partition_service.list_partition_tasks(partition_id)
    }


def _row(partition_id: str, task_id: str) -> dict[str, Any]:
    rows = {r["task_id"]: r for r in partition_service.list_partition_tasks(partition_id)}
    return dict(rows[task_id])


def _crashing_create_run_row(monkeypatch) -> dict[str, bool]:
    """Patch `_create_run_row` to crash, returning a toggle that stops it.

    Deliberately a toggle rather than `monkeypatch.undo()`: pytest hands every
    fixture and the test itself the SAME function-scoped `monkeypatch`, so
    `undo()` would also revert `conftest._isolate_state_db`'s `DB_PATH`
    redirection and point the rest of the test at the developer's real
    `./state.db`.
    """
    crash = {"on": True}
    original = partition_service._create_run_row

    def _maybe_crash(**kwargs: Any) -> int:
        if crash["on"]:
            raise _Crash("process died between claim and create")
        return original(**kwargs)

    monkeypatch.setattr(partition_service, "_create_run_row", _maybe_crash)
    return crash


class TestCrashBetweenClaimAndCreate:
    def test_a_crash_leaves_a_claimed_row_with_no_run_id(self, live_config, monkeypatch) -> None:
        partition_id = _ratified(_plan(_task("a")))
        _crashing_create_run_row(monkeypatch)
        orch = RecordingOrchestrator()

        with pytest.raises(_Crash):
            partition_service.dispatch_partition(partition_id, orchestrator=orch)

        row = _row(partition_id, "a")
        assert row["status"] == "claimed"
        assert row["run_id"] is None
        assert row["claimed_by"]
        # Nothing ran: the claim is the fence, and it comes first.
        assert orch.calls == []

    def test_the_reconciler_rewinds_that_row_to_pending_exactly_once(
        self, live_config, monkeypatch
    ) -> None:
        partition_id = _ratified(_plan(_task("a")))
        crash = _crashing_create_run_row(monkeypatch)
        with pytest.raises(_Crash):
            partition_service.dispatch_partition(partition_id, orchestrator=RecordingOrchestrator())
        crash["on"] = False

        first = partition_service.reconcile_stale_claims(older_than_seconds=0)
        second = partition_service.reconcile_stale_claims(older_than_seconds=0)

        assert first == ((partition_id, "a"),)
        # EXACTLY once: the conditional `claimed -> pending` UPDATE can only
        # be won by one caller, so a second sweep reports nothing.
        assert second == ()
        row = _row(partition_id, "a")
        assert row["status"] == "pending"
        assert row["claimed_by"] is None

    def test_after_reconciliation_a_resumed_dispatch_runs_the_task_exactly_once(
        self, live_config, monkeypatch
    ) -> None:
        partition_id = _ratified(_plan(_task("a")))
        crash = _crashing_create_run_row(monkeypatch)
        with pytest.raises(_Crash):
            partition_service.dispatch_partition(partition_id, orchestrator=RecordingOrchestrator())
        crash["on"] = False
        partition_service.reconcile_stale_claims(older_than_seconds=0)

        orch = RecordingOrchestrator()
        partition_service.dispatch_partition(partition_id, orchestrator=orch, resume=True)

        assert len(orch.calls) == 1
        assert _statuses(partition_id) == {"a": "committed"}

    def test_reconciliation_never_double_dispatches_a_task_that_already_started(
        self, live_config
    ) -> None:
        """A claim that reached `mark_task_running` has a `run_id`, so the
        `AND run_id IS NULL` half of `release_stale_claim` refuses it -- which
        is the whole reason recovery cannot double-dispatch."""
        partition_id = _ratified(_plan(_task("a")))
        assert partition_service.claim_task(partition_id, "a", claimed_by="owner-1")
        assert partition_service.mark_task_running(
            partition_id, "a", claimed_by="owner-1", run_id=4242
        )
        # Force it back to `claimed` WITH its run_id intact -- the shape a
        # naive "any claimed row is stale" sweeper would wrongly rewind.
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "UPDATE partition_tasks SET status='claimed' WHERE partition_id=? AND task_id=?"
                ),
                (partition_id, "a"),
            )

        assert partition_service.reconcile_stale_claims(older_than_seconds=0) == ()
        assert _row(partition_id, "a")["status"] == "claimed"

    def test_a_graceful_create_failure_rewinds_immediately_without_the_reconciler(
        self, live_config, monkeypatch
    ) -> None:
        """A recoverable (non-crash) failure between claim and create must
        not need a restart to unstick: the dispatcher rewinds its own claim."""
        partition_id = _ratified(_plan(_task("a")))
        monkeypatch.setattr(
            partition_service,
            "_create_run_row",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db busy")),
        )
        orch = RecordingOrchestrator()

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.calls == []
        assert outcome.dispatched == ()
        assert _row(partition_id, "a")["status"] == "pending"


class TestStalenessThreshold:
    def test_a_fresh_claim_is_not_swept_under_the_default_threshold(self, live_config) -> None:
        """Rewinding a claim a LIVE dispatcher made microseconds ago would
        hand the same task to a second dispatcher -- the exact double dispatch
        this design exists to prevent."""
        partition_id = _ratified(_plan(_task("a")))
        assert partition_service.claim_task(partition_id, "a", claimed_by="owner-1")

        assert partition_service.reconcile_stale_claims() == ()
        assert _row(partition_id, "a")["status"] == "claimed"

    def test_an_old_claim_is_swept(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a")))
        assert partition_service.claim_task(partition_id, "a", claimed_by="owner-1")
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "UPDATE partition_tasks SET claimed_at='2020-01-01 00:00:00' "
                    "WHERE partition_id=? AND task_id=?"
                ),
                (partition_id, "a"),
            )

        assert partition_service.reconcile_stale_claims() == ((partition_id, "a"),)
        assert _row(partition_id, "a")["status"] == "pending"

    def test_an_unparseable_claimed_at_is_left_alone(self, live_config) -> None:
        """Fail-closed: a claim that MIGHT still be owned is never released.
        A stuck row an operator can see beats a silent double dispatch."""
        partition_id = _ratified(_plan(_task("a")))
        assert partition_service.claim_task(partition_id, "a", claimed_by="owner-1")
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "UPDATE partition_tasks SET claimed_at='not-a-timestamp' "
                    "WHERE partition_id=? AND task_id=?"
                ),
                (partition_id, "a"),
            )

        assert partition_service.reconcile_stale_claims() == ()
        assert _row(partition_id, "a")["status"] == "claimed"

    def test_a_null_claimed_at_is_left_alone_under_a_positive_threshold(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a")))
        assert partition_service.claim_task(partition_id, "a", claimed_by="owner-1")
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "UPDATE partition_tasks SET claimed_at=NULL WHERE partition_id=? AND task_id=?"
                ),
                (partition_id, "a"),
            )

        assert partition_service.reconcile_stale_claims() == ()
        assert _row(partition_id, "a")["status"] == "claimed"


class TestReconcilerScope:
    def test_only_claimed_rows_are_candidates(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("pending-one"), _task("committed-one")))
        assert partition_service.claim_task(partition_id, "committed-one", claimed_by="o")
        assert partition_service.mark_task_running(
            partition_id, "committed-one", claimed_by="o", run_id=1
        )
        assert partition_service.mark_task_committed(partition_id, "committed-one", claimed_by="o")

        assert partition_service.reconcile_stale_claims(older_than_seconds=0) == ()
        assert _statuses(partition_id) == {
            "pending-one": "pending",
            "committed-one": "committed",
        }

    def test_the_sweep_can_be_scoped_to_one_tenant(self, live_config) -> None:
        mine = _ratified(_plan(_task("a")), tenant="acme")
        theirs = _ratified(_plan(_task("a")), tenant="other-co")
        assert partition_service.claim_task(mine, "a", claimed_by="o")
        assert partition_service.claim_task(theirs, "a", claimed_by="o")

        released = partition_service.reconcile_stale_claims(tenant="acme", older_than_seconds=0)

        assert released == ((mine, "a"),)
        assert _row(theirs, "a")["status"] == "claimed"

    def test_the_reconciler_never_touches_partition_status(self, live_config) -> None:
        """`dispatching` never auto-completes (spec section 8) -- recovering a
        task says nothing about whether the partition's work is done."""
        partition_id = _ratified(_plan(_task("a")))
        assert partition_service.mark_partition_dispatching(partition_id)
        assert partition_service.claim_task(partition_id, "a", claimed_by="o")

        partition_service.reconcile_stale_claims(older_than_seconds=0)

        assert partition_service.get_partition(partition_id)["status"] == "dispatching"

    def test_reconciling_an_empty_journal_is_a_no_op(self, live_config) -> None:
        state_service.init_db()
        assert partition_service.reconcile_stale_claims(older_than_seconds=0) == ()
