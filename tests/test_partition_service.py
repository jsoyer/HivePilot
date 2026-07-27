"""Tests for `hivepilot.services.partition_service` -- the durable partition
journal (propose -> ratify -> dispatch PRD, Sprint 2, spec section 8).

The ratification GATE's fail-closed validation ORDER lives in the sibling
module `tests/test_partition_ratify_validation.py`; this file covers
persistence, the conditional-UPDATE state machine (`rowcount == 1`),
idempotency, the audit trail, and the additive `autopilot_queue.kind`
column that keeps `drain_one` away from partition tasks.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hivepilot.partition import load_partition
from hivepilot.services import autopilot_queue, partition_service, state_service

# ---------------------------------------------------------------------------
# Live-config fixture -- real YAML files resolved through the REAL
# `settings.resolve_config_path` chain, not a mock. The gate's whole point is
# that it reads LIVE config, so mocking the config loader out would test the
# opposite of the property under test.
# ---------------------------------------------------------------------------

PROJECTS_YAML = """
projects:
  acme-api:
    path: /tmp/acme-api
    modules:
      core: apps/core
"""

PIPELINES_YAML = """
pipelines:
  bugfix:
    description: fix a bug
    stages:
      - name: fix
        task: implement
  ship-it:
    description: ship a change
    stages:
      - name: ship
        task: ship
"""

TASKS_YAML = """
tasks:
  implement:
    description: implement something
  ship:
    description: push a branch and open a PR
    git:
      push: true
      create_pr: true
"""

POLICIES_YAML = """
policies:
  default:
    require_approval: true
  projects:
    acme-api:
      outward_actions:
        - git_push
        - forge_pr
      budget_daily_usd: 100.0
      max_partition_cost_usd: 10.0
      max_task_wall_clock_seconds: 3600
"""


@pytest.fixture
def live_config(tmp_path, monkeypatch):
    """Point `settings.config_repo` at a tmp dir holding the four config
    files this module reads, so every `resolve_config_path` lookup lands
    there instead of on the repo's own root-level config."""
    from hivepilot.config import settings
    from hivepilot.services import policy_service

    (tmp_path / "projects.yaml").write_text(PROJECTS_YAML, encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text(PIPELINES_YAML, encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    (tmp_path / "policies.yaml").write_text(POLICIES_YAML, encoding="utf-8")
    monkeypatch.setattr(settings, "config_repo", str(tmp_path), raising=False)
    policy_service.reload_policies()
    yield tmp_path
    policy_service.reload_policies()


@pytest.fixture(autouse=True)
def _zero_spend(monkeypatch):
    """Deterministic daily spend. The real hook reads analytics; every test
    here that cares about the budget sets its own value."""
    monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *, tenant="default": 0.0)


def _task(task_id: str, **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "project": "acme-api",
        "pipeline": "bugfix",
        "prompt": f"do the {task_id} work",
        "depends_on": [],
        "budget": {"wall_clock_seconds": 1500, "cost_usd": 1.5},
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


def _json(plan: dict[str, Any]) -> str:
    return json.dumps(plan)


def _create(**kwargs: Any) -> tuple[str, str]:
    """Create a proposed partition; return (partition_id, plan_json)."""
    plan_json = kwargs.pop("plan_json", None) or _json(_plan())
    pid = partition_service.create_partition(plan_json=plan_json, **kwargs)
    return pid, plan_json


# ---------------------------------------------------------------------------
# Schema + persistence
# ---------------------------------------------------------------------------


class TestSchema:
    def test_init_db_creates_both_journal_tables_with_the_specified_columns(self) -> None:
        """The two tables belong to `state_service.init_db` (like every
        other table), not to a private DDL inside `partition_service` --
        so a fresh DB has them before any partition code runs."""
        from hivepilot.services import db

        state_service.init_db()
        with db.connect() as conn:
            partitions = {r[1] for r in conn.execute("PRAGMA table_info(partitions)").fetchall()}
            tasks = {r[1] for r in conn.execute("PRAGMA table_info(partition_tasks)").fetchall()}
        assert {
            "id",
            "tenant",
            "source_kind",
            "source_ref",
            "source_digest",
            "proposer_run_id",
            "proposed_json",
            "proposed_digest",
            "ratified_json",
            "ratified_digest",
            "ratified_diff",
            "outward_consent",
            "status",
            "ratified_by",
            "ratified_at",
            "created_ts",
            "updated_ts",
        } <= partitions
        assert {
            "partition_id",
            "task_id",
            "queue_id",
            "run_id",
            "attempt",
            "status",
            "claimed_by",
            "claimed_at",
            "pr_url",
            "cost_usd",
            "wall_clock_seconds",
            "updated_ts",
        } <= tasks

    def test_partition_tasks_is_keyed_by_partition_and_task(self) -> None:
        """A partition PRECEDES and SPANS N runs, which is exactly why the
        `approvals` table (PRIMARY KEY(run_id)) could not be reused."""
        from hivepilot.services import db

        state_service.init_db()
        with db.connect() as conn:
            pk = {
                r[1] for r in conn.execute("PRAGMA table_info(partition_tasks)").fetchall() if r[5]
            }
        assert pk == {"partition_id", "task_id"}

    def test_init_db_is_idempotent(self) -> None:
        state_service.init_db()
        state_service.init_db()
        assert partition_service.list_partitions() == []


class TestPartitionPersistence:
    def test_create_partition_persists_proposal_verbatim(self) -> None:
        plan_json = _json(_plan())
        pid = partition_service.create_partition(plan_json=plan_json)

        row = partition_service.get_partition(pid)
        assert row is not None
        assert row["status"] == "proposed"
        # Verbatim: never a re-serialized round-trip, so the operator reviews
        # exactly what the proposer emitted.
        assert row["proposed_json"] == plan_json
        assert row["source_kind"] == "text"
        assert row["source_ref"] == "docs/bug-1234.md"
        assert row["source_digest"] == "sha256:aaa"
        assert row["proposer_run_id"] == 4711
        assert row["ratified_json"] is None
        assert row["outward_consent"] == 0

    def test_create_partition_rejects_a_plan_that_does_not_parse(self) -> None:
        from hivepilot.partition import MalformedPartitionError

        with pytest.raises(MalformedPartitionError):
            partition_service.create_partition(plan_json="{not json")
        assert partition_service.list_partitions() == []

    def test_digest_is_the_shared_sha256_shape(self) -> None:
        pid, _ = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        assert row["proposed_digest"].startswith("sha256:")
        assert len(row["proposed_digest"]) == len("sha256:") + 64

    def test_digest_is_whitespace_insensitive(self) -> None:
        """A whitespace-only reformat must not invalidate a browser tab's
        `expected_digest` -- the digest is taken over the CANONICAL form."""
        compact = json.dumps(_plan(), separators=(",", ":"))
        pretty = json.dumps(_plan(), indent=4)
        assert partition_service.partition_digest(compact) == partition_service.partition_digest(
            pretty
        )

    def test_digest_changes_when_the_plan_changes(self) -> None:
        a = _json(_plan(_task("t1")))
        b = _json(_plan(_task("t1", prompt="something else entirely")))
        assert partition_service.partition_digest(a) != partition_service.partition_digest(b)

    def test_get_partition_is_tenant_scoped(self) -> None:
        pid, _ = _create(tenant="tenant-a")
        assert partition_service.get_partition(pid, tenant="tenant-a") is not None
        # A cross-tenant id is indistinguishable from a missing one.
        assert partition_service.get_partition(pid, tenant="tenant-b") is None
        assert partition_service.get_partition(pid, tenant=None) is not None

    def test_list_partitions_filters_by_tenant_and_status(self) -> None:
        _create(tenant="tenant-a")
        _create(tenant="tenant-b")
        assert len(partition_service.list_partitions(tenant="tenant-a")) == 1
        assert len(partition_service.list_partitions(tenant=None)) == 2
        assert partition_service.list_partitions(tenant=None, status="ratified") == []


# ---------------------------------------------------------------------------
# Partition state machine -- conditional UPDATE, rowcount == 1
# ---------------------------------------------------------------------------


class TestPartitionTransitions:
    def test_dispatching_requires_ratified_and_never_fires_from_proposed(self) -> None:
        """The persistence half of "a partition never dispatches without
        human ratification"."""
        pid, _ = _create()
        assert partition_service.mark_partition_dispatching(pid) is False
        row = partition_service.get_partition(pid)
        assert row is not None and row["status"] == "proposed"

    def test_full_happy_path_transitions_each_win_exactly_once(self, live_config) -> None:
        pid, plan_json = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="alice",
            expected_digest=row["proposed_digest"],
        )
        assert partition_service.mark_partition_dispatching(pid) is True
        assert partition_service.mark_partition_dispatching(pid) is False  # already moved on
        assert partition_service.mark_partition_completed(pid) is True
        assert partition_service.mark_partition_completed(pid) is False

    def test_completed_never_reachable_from_ratified(self, live_config) -> None:
        """`dispatching` never auto-completes: "nothing is running any more"
        and "the work finished" are different claims."""
        pid, plan_json = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="alice",
            expected_digest=row["proposed_digest"],
        )
        assert partition_service.mark_partition_completed(pid) is False

    def test_veto_only_applies_to_a_proposed_partition(self, live_config) -> None:
        pid, plan_json = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        assert partition_service.veto_partition(pid, actor="alice") is True
        assert partition_service.veto_partition(pid, actor="alice") is False
        # And a vetoed partition can no longer be ratified.
        outcome = partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="mallory",
            expected_digest=row["proposed_digest"],
        )
        assert outcome.idempotent is True
        assert outcome.status == "vetoed"

    def test_veto_is_audited(self) -> None:
        pid, _ = _create()
        partition_service.veto_partition(pid, actor="alice")
        actions = [i["action"] for i in state_service.list_recent_interactions(limit=10)]
        assert "partition.veto" in actions

    def test_expire_only_applies_to_a_proposed_partition(self, live_config) -> None:
        pid, plan_json = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="alice",
            expected_digest=row["proposed_digest"],
        )
        # A ratified partition can never expire.
        assert partition_service.expire_partition(pid) is False

    def test_transition_to_an_unknown_status_is_rejected(self) -> None:
        pid, _ = _create()
        with pytest.raises(ValueError):
            partition_service._transition_partition(
                pid, from_status="proposed", to_status="nonsense"
            )


# ---------------------------------------------------------------------------
# Task journal -- claim / running / committed / failed / skipped / cancelled
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(live_config):
    """A ratified partition with three independent pending task rows."""
    plan_json = _json(_plan(_task("a"), _task("b"), _task("c")))
    pid = partition_service.create_partition(plan_json=plan_json)
    row = partition_service.get_partition(pid)
    assert row is not None
    partition_service.ratify_partition(
        pid,
        partition_json=plan_json,
        outward_consent=False,
        approver="alice",
        expected_digest=row["proposed_digest"],
    )
    return pid


def _status(pid: str, task_id: str) -> str:
    rows = {r["task_id"]: r for r in partition_service.list_partition_tasks(pid)}
    return str(rows[task_id]["status"])


class TestTaskJournal:
    def test_ratify_writes_one_pending_row_per_task_with_its_wall_clock(self, journal) -> None:
        rows = partition_service.list_partition_tasks(journal)
        assert [r["task_id"] for r in rows] == ["a", "b", "c"]
        assert {r["status"] for r in rows} == {"pending"}
        assert {r["wall_clock_seconds"] for r in rows} == {1500}
        assert {r["attempt"] for r in rows} == {0}
        assert {r["run_id"] for r in rows} == {None}

    def test_claim_is_won_by_exactly_one_caller(self, journal) -> None:
        assert partition_service.claim_task(journal, "a", claimed_by="worker-1") is True
        # A second claimer loses -- the row is no longer `pending`.
        assert partition_service.claim_task(journal, "a", claimed_by="worker-2") is False
        assert _status(journal, "a") == "claimed"

    def test_running_requires_the_claim_owner(self, journal) -> None:
        partition_service.claim_task(journal, "a", claimed_by="worker-1")
        # The claim race LOSER must not be able to start the task, even
        # though it can see `status='claimed'` -- the TOCTOU hole
        # `mark_swarm_event_running` closed.
        assert (
            partition_service.mark_task_running(journal, "a", claimed_by="worker-2", run_id=99)
            is False
        )
        assert _status(journal, "a") == "claimed"
        assert (
            partition_service.mark_task_running(journal, "a", claimed_by="worker-1", run_id=99)
            is True
        )
        assert _status(journal, "a") == "running"

    def test_running_is_exactly_once_even_for_the_owner(self, journal) -> None:
        partition_service.claim_task(journal, "a", claimed_by="worker-1")
        assert partition_service.mark_task_running(journal, "a", claimed_by="worker-1") is True
        assert partition_service.mark_task_running(journal, "a", claimed_by="worker-1") is False

    def test_running_cannot_skip_the_claim(self, journal) -> None:
        assert partition_service.mark_task_running(journal, "a", claimed_by="worker-1") is False
        assert _status(journal, "a") == "pending"

    def test_committed_records_a_real_pr_url_and_cost(self, journal) -> None:
        partition_service.claim_task(journal, "a", claimed_by="w")
        partition_service.mark_task_running(journal, "a", claimed_by="w", run_id=7)
        assert (
            partition_service.mark_task_committed(
                journal, "a", claimed_by="w", pr_url="https://forge/pr/1", cost_usd=0.42
            )
            is True
        )
        row = {r["task_id"]: r for r in partition_service.list_partition_tasks(journal)}["a"]
        assert row["status"] == "committed"
        assert row["pr_url"] == "https://forge/pr/1"
        assert row["cost_usd"] == 0.42
        assert row["run_id"] == 7

    def test_committed_leaves_pr_url_null_when_the_forge_cannot_produce_one(self, journal) -> None:
        """NULL, never a fabricated URL."""
        partition_service.claim_task(journal, "a", claimed_by="w")
        partition_service.mark_task_running(journal, "a", claimed_by="w", run_id=7)
        partition_service.mark_task_committed(journal, "a", claimed_by="w", pr_url=None)
        row = {r["task_id"]: r for r in partition_service.list_partition_tasks(journal)}["a"]
        assert row["status"] == "committed"
        assert row["pr_url"] is None

    def test_committed_requires_the_claim_owner(self, journal) -> None:
        partition_service.claim_task(journal, "a", claimed_by="w")
        partition_service.mark_task_running(journal, "a", claimed_by="w")
        assert partition_service.mark_task_committed(journal, "a", claimed_by="other") is False

    def test_failed_applies_from_claimed_or_running_for_the_owner(self, journal) -> None:
        partition_service.claim_task(journal, "a", claimed_by="w")
        assert partition_service.mark_task_failed(journal, "a", claimed_by="other") is False
        assert partition_service.mark_task_failed(journal, "a", claimed_by="w") is True
        assert _status(journal, "a") == "failed"

    def test_skipped_is_a_distinct_terminal_state_from_failed(self, journal) -> None:
        """A dependent of a failed task is `skipped`, never `failed` --
        recording it as `failed` would lie about what happened."""
        partition_service.claim_task(journal, "a", claimed_by="w")
        partition_service.mark_task_failed(journal, "a", claimed_by="w")
        assert partition_service.mark_task_skipped(journal, "b") is True
        assert _status(journal, "b") == "skipped"
        assert _status(journal, "a") == "failed"

    def test_skipped_never_rewrites_a_task_already_underway(self, journal) -> None:
        partition_service.claim_task(journal, "a", claimed_by="w")
        assert partition_service.mark_task_skipped(journal, "a") is False
        assert _status(journal, "a") == "claimed"

    def test_cancel_applies_to_pending_and_claimed_but_never_running(self, journal) -> None:
        assert partition_service.mark_task_cancelled(journal, "a") is True
        partition_service.claim_task(journal, "b", claimed_by="w")
        assert partition_service.mark_task_cancelled(journal, "b") is True
        partition_service.claim_task(journal, "c", claimed_by="w")
        partition_service.mark_task_running(journal, "c", claimed_by="w", run_id=3)
        # Running agents are never killed -- only cooperatively cancelled.
        assert partition_service.mark_task_cancelled(journal, "c") is False
        assert _status(journal, "c") == "running"


class TestClaimBeforeCreateRecovery:
    def test_a_claim_with_no_run_is_released_back_to_pending_exactly_once(self, journal) -> None:
        """Simulates a crash between `claim_task` and the run-row creation:
        a visible `claimed` row with `run_id IS NULL`."""
        partition_service.claim_task(journal, "a", claimed_by="dead-worker")
        assert partition_service.release_stale_claim(journal, "a") is True
        assert _status(journal, "a") == "pending"
        # Exactly once: a second sweep finds nothing to release.
        assert partition_service.release_stale_claim(journal, "a") is False

    def test_a_claim_that_did_produce_a_run_is_never_rewound(self, journal) -> None:
        """The `AND run_id IS NULL` half of the recovery UPDATE is what makes
        it impossible for a startup sweep to double-dispatch."""
        partition_service.claim_task(journal, "a", claimed_by="w")
        partition_service.mark_task_running(journal, "a", claimed_by="w", run_id=11)
        assert partition_service.release_stale_claim(journal, "a") is False
        assert _status(journal, "a") == "running"

    def test_released_row_is_reclaimable_by_a_new_worker(self, journal) -> None:
        partition_service.claim_task(journal, "a", claimed_by="dead-worker")
        partition_service.release_stale_claim(journal, "a")
        assert partition_service.claim_task(journal, "a", claimed_by="fresh-worker") is True


# ---------------------------------------------------------------------------
# Idempotency + audit
# ---------------------------------------------------------------------------


class TestRatifyIdempotencyAndAudit:
    def test_second_ratify_is_a_noop_and_never_a_second_dispatch(self, live_config) -> None:
        pid, plan_json = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        digest = row["proposed_digest"]

        first = partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="alice",
            expected_digest=digest,
        )
        second = partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="bob",
            expected_digest=digest,
        )

        assert first.idempotent is False
        assert second.idempotent is True
        assert second.status == "ratified"
        # The winner remains the approver of record -- a second call never
        # overwrites the audit trail.
        after = partition_service.get_partition(pid)
        assert after is not None
        assert after["ratified_by"] == "alice"
        # Exactly one `partition.ratify` interaction, never two.
        actions = [i["action"] for i in state_service.list_recent_interactions(limit=50)]
        assert actions.count("partition.ratify") == 1

    def test_second_ratify_never_resets_task_rows_already_underway(self, live_config) -> None:
        plan_json = _json(_plan(_task("a"), _task("b")))
        pid = partition_service.create_partition(plan_json=plan_json)
        row = partition_service.get_partition(pid)
        assert row is not None
        digest = row["proposed_digest"]
        partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="alice",
            expected_digest=digest,
        )
        partition_service.claim_task(pid, "a", claimed_by="w")
        partition_service.mark_task_running(pid, "a", claimed_by="w", run_id=5)

        partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="bob",
            expected_digest=digest,
        )
        assert _status(pid, "a") == "running"

    def test_the_diff_actor_and_consent_land_in_interactions(self, live_config) -> None:
        proposed = _plan(_task("a", budget={"wall_clock_seconds": 1500, "cost_usd": 1.5}))
        pid = partition_service.create_partition(plan_json=_json(proposed))
        row = partition_service.get_partition(pid)
        assert row is not None

        edited = _plan(_task("a", budget={"wall_clock_seconds": 900, "cost_usd": 1.5}))
        outcome = partition_service.ratify_partition(
            pid,
            partition_json=_json(edited),
            outward_consent=True,
            approver="alice@example.com",
            expected_digest=row["proposed_digest"],
        )

        assert "1500" in outcome.diff and "900" in outcome.diff
        entry = next(
            i
            for i in state_service.list_recent_interactions(limit=50)
            if i["action"] == "partition.ratify"
        )
        assert entry["actor"] == "alice@example.com"
        assert entry["target"] == pid
        assert "900" in entry["summary"]  # the unified diff itself
        meta = json.loads(entry["metadata"])
        assert meta["outward_consent"] is True
        assert meta["edited"] is True
        assert meta["task_count"] == 1

    def test_metadata_never_carries_the_plan_blobs(self, live_config) -> None:
        """`record_interaction` redacts `summary` but stores `metadata`
        verbatim, so metadata must stay structured and non-secret."""
        pid, plan_json = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="alice",
            expected_digest=row["proposed_digest"],
        )
        entry = next(
            i
            for i in state_service.list_recent_interactions(limit=50)
            if i["action"] == "partition.ratify"
        )
        meta = json.loads(entry["metadata"])
        assert "proposed_json" not in meta
        assert "do the t1 work" not in entry["metadata"]

    def test_both_blobs_both_digests_and_the_diff_are_persisted(self, live_config) -> None:
        proposed = _plan(_task("a"))
        pid = partition_service.create_partition(plan_json=_json(proposed))
        row = partition_service.get_partition(pid)
        assert row is not None
        edited_json = _json(_plan(_task("a", title="Retitled")))

        partition_service.ratify_partition(
            pid,
            partition_json=edited_json,
            outward_consent=False,
            approver="alice",
            expected_digest=row["proposed_digest"],
        )

        after = partition_service.get_partition(pid)
        assert after is not None
        assert after["proposed_json"] == _json(proposed)
        assert after["ratified_json"] == edited_json
        assert after["proposed_digest"] != after["ratified_digest"]
        assert "Retitled" in after["ratified_diff"]
        assert after["ratified_by"] == "alice"
        assert after["ratified_at"] is not None

    def test_unedited_ratification_records_an_empty_diff_honestly(self, live_config) -> None:
        pid, plan_json = _create()
        row = partition_service.get_partition(pid)
        assert row is not None
        outcome = partition_service.ratify_partition(
            pid,
            partition_json=plan_json,
            outward_consent=False,
            approver="alice",
            expected_digest=row["proposed_digest"],
        )
        assert outcome.diff == ""
        entry = next(
            i
            for i in state_service.list_recent_interactions(limit=50)
            if i["action"] == "partition.ratify"
        )
        assert json.loads(entry["metadata"])["edited"] is False

    def test_a_source_digest_drift_warns_but_never_denies(self, live_config) -> None:
        proposed = _plan(_task("a"))
        pid = partition_service.create_partition(plan_json=_json(proposed))
        row = partition_service.get_partition(pid)
        assert row is not None
        drifted = _plan(_task("a"))
        drifted["source"] = dict(drifted["source"], digest="sha256:bbb")

        outcome = partition_service.ratify_partition(
            pid,
            partition_json=_json(drifted),
            outward_consent=False,
            approver="alice",
            expected_digest=row["proposed_digest"],
        )
        assert outcome.status == "ratified"
        assert any("drifted" in w for w in outcome.warnings)


# ---------------------------------------------------------------------------
# `autopilot_queue.kind` -- drain_one must never touch partition tasks
# ---------------------------------------------------------------------------


class TestQueueKindSeparation:
    def test_enqueue_defaults_to_objective_so_existing_callers_are_unchanged(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "bugfix", "why")
        item = autopilot_queue.list_queue()[0]
        assert item.id == item_id
        assert item.kind == autopilot_queue.KIND_OBJECTIVE

    def test_next_dispatchable_never_returns_a_partition_task(self) -> None:
        autopilot_queue.enqueue(
            "acme-api", "bugfix", "partition work", kind=autopilot_queue.KIND_PARTITION_TASK
        )
        assert autopilot_queue.next_dispatchable() is None

    def test_next_dispatchable_skips_partition_tasks_and_picks_the_objective(self) -> None:
        autopilot_queue.enqueue(
            "acme-api", "bugfix", "partition work", kind=autopilot_queue.KIND_PARTITION_TASK
        )
        objective_id = autopilot_queue.enqueue("acme-api", "bugfix", "an objective")
        item = autopilot_queue.next_dispatchable()
        assert item is not None
        assert item.id == objective_id
        assert item.kind == autopilot_queue.KIND_OBJECTIVE

    def test_promoted_partition_task_is_still_invisible_to_the_drain(self) -> None:
        """Even a `queued` partition task -- the state `next_dispatchable`
        prefers -- must stay out of the unattended drain's reach."""
        qid = autopilot_queue.enqueue(
            "acme-api", "bugfix", "partition work", kind=autopilot_queue.KIND_PARTITION_TASK
        )
        autopilot_queue.promote(qid)
        assert autopilot_queue.next_dispatchable() is None

    def test_list_queue_can_filter_by_kind_and_defaults_to_every_kind(self) -> None:
        autopilot_queue.enqueue("acme-api", "bugfix", "o")
        autopilot_queue.enqueue("acme-api", "bugfix", "p", kind=autopilot_queue.KIND_PARTITION_TASK)
        assert len(autopilot_queue.list_queue()) == 2
        assert len(autopilot_queue.list_queue(kind=autopilot_queue.KIND_OBJECTIVE)) == 1
        assert len(autopilot_queue.list_queue(kind=autopilot_queue.KIND_PARTITION_TASK)) == 1

    def test_an_unknown_kind_is_rejected_rather_than_silently_written(self) -> None:
        """A row with an unrecognized kind would be invisible to BOTH
        drains -- a silent black hole."""
        with pytest.raises(ValueError):
            autopilot_queue.enqueue("acme-api", "bugfix", "x", kind="not-a-kind")

    def test_drain_one_leaves_a_partition_task_untouched(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        qid = autopilot_queue.enqueue(
            "acme-api", "bugfix", "partition work", kind=autopilot_queue.KIND_PARTITION_TASK
        )
        orchestrator = MagicMock()
        assert autopilot_queue.drain_one(orchestrator) is None
        orchestrator.run_pipeline.assert_not_called()
        assert autopilot_queue.list_queue()[0].id == qid
        assert autopilot_queue.list_queue()[0].state == "proposed"


class TestOutwardActionsResolution:
    def test_wrapper_and_generalized_function_agree(self, live_config) -> None:
        assert autopilot_queue.pipeline_outward_actions("bugfix") == frozenset()
        assert autopilot_queue.pipeline_would_auto_merge("bugfix") is False
        assert autopilot_queue.pipeline_outward_actions("ship-it") == frozenset(
            {"git_push", "forge_pr"}
        )
        assert autopilot_queue.pipeline_would_auto_merge("ship-it") is False

    def test_unresolvable_config_fails_closed_to_the_full_set(self, live_config) -> None:
        assert autopilot_queue.pipeline_outward_actions("no-such-pipeline") == (
            autopilot_queue.OUTWARD_ACTIONS
        )
        # ... which is exactly why the wrapper still refuses it.
        assert autopilot_queue.pipeline_would_auto_merge("no-such-pipeline") is True

    def test_commits_vault_stage_resolves_to_vault_write(self, tmp_path, monkeypatch) -> None:
        from hivepilot.config import settings

        (tmp_path / "pipelines.yaml").write_text(
            "pipelines:\n  vaulted:\n    description: vaulted\n    stages:\n"
            "      - name: note\n        task: implement\n        commits_vault: true\n",
            encoding="utf-8",
        )
        (tmp_path / "tasks.yaml").write_text(
            "tasks:\n  implement:\n    description: x\n", encoding="utf-8"
        )
        monkeypatch.setattr(settings, "config_repo", str(tmp_path), raising=False)
        assert autopilot_queue.pipeline_outward_actions("vaulted") == frozenset({"vault_write"})


# ---------------------------------------------------------------------------
# `Orchestrator.approve_run`'s third route
# ---------------------------------------------------------------------------


class TestApproveRunPartitionRoute:
    """One routing entry point, three routes -- so Pollen, Telegram, Slack,
    Discord and the CLI can never diverge on how a partition is ratified."""

    @staticmethod
    def _orchestrator():
        from hivepilot.orchestrator import Orchestrator

        return Orchestrator()

    @staticmethod
    def _park(pid: str, run_id: int = 900, **meta: Any) -> None:
        state_service.record_approval_request(
            run_id,
            "-",
            "partition_ratify",
            {"kind": "partition_ratify", "partition_id": pid, **meta},
        )

    def test_approve_ratifies_the_stored_plan(self, live_config) -> None:
        pid, _ = _create()
        self._park(pid)
        result = self._orchestrator().approve_run(run_id=900, approve=True, approver="alice")
        assert result.success is True
        row = partition_service.get_partition(pid)
        assert row is not None
        assert row["status"] == "ratified"
        assert row["ratified_by"] == "alice"

    def test_approve_can_never_grant_outward_consent(self, live_config) -> None:
        """A generic "Approve" button on a chat platform must not be able to
        authorize pushing branches and opening PRs: this route has no
        consent argument, so an outward plan is refused here and the
        operator must use the dedicated ratify surface."""
        plan_json = _json(_plan(_task("t1", pipeline="ship-it")))
        pid = partition_service.create_partition(plan_json=plan_json)
        self._park(pid)
        result = self._orchestrator().approve_run(run_id=900, approve=True, approver="alice")
        assert result.success is False
        assert "outward_consent" in (result.detail or "")
        row = partition_service.get_partition(pid)
        assert row is not None
        assert row["status"] == "proposed"
        assert partition_service.list_partition_tasks(pid) == []

    def test_deny_vetoes_the_partition(self, live_config) -> None:
        pid, _ = _create()
        self._park(pid)
        result = self._orchestrator().approve_run(run_id=900, approve=False, approver="alice")
        assert result.success is False
        row = partition_service.get_partition(pid)
        assert row is not None
        assert row["status"] == "vetoed"

    def test_an_approval_row_with_no_partition_id_fails_closed(self, live_config) -> None:
        """Never guess which partition was meant."""
        state_service.record_approval_request(
            901, "-", "partition_ratify", {"kind": "partition_ratify"}
        )
        result = self._orchestrator().approve_run(run_id=901, approve=True, approver="alice")
        assert result.success is False
        assert "no partition_id" in (result.detail or "")

    def test_an_unknown_partition_id_is_refused_not_raised(self, live_config) -> None:
        self._park("no-such-partition", run_id=902)
        result = self._orchestrator().approve_run(run_id=902, approve=True, approver="alice")
        assert result.success is False

    def test_a_resolved_approval_row_is_still_refused_up_front(self, live_config) -> None:
        """The `pending` precondition applies to this route exactly as it
        does to the other two."""
        pid, _ = _create()
        self._park(pid)
        # One instance, reused: a second real `Orchestrator()` in the same
        # test re-scans the plugins directory and collides with itself (see
        # conftest's `_isolate_runner_and_notifier_maps`).
        orch = self._orchestrator()
        orch.approve_run(run_id=900, approve=True, approver="alice")
        with pytest.raises(ValueError):
            orch.approve_run(run_id=900, approve=True, approver="bob")


def test_partition_plan_round_trips_through_the_shared_contract(live_config) -> None:
    """`insert_task_rows` reads the parsed plan, so the journal and the gate
    can never disagree about which tasks exist."""
    plan_json = _json(_plan(_task("x"), _task("y", depends_on=["x"])))
    plan = load_partition(plan_json)
    pid = partition_service.create_partition(plan_json=plan_json)
    assert partition_service.insert_task_rows(pid, plan) == ("x", "y")
    assert [r["task_id"] for r in partition_service.list_partition_tasks(pid)] == ["x", "y"]
