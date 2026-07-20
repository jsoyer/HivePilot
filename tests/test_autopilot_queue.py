"""Tests for hivepilot.services.autopilot_queue — the guarded objective
queue and its fail-closed autopilot_gate.

Covers: queue lifecycle (enqueue/list/promote/veto/mark), the gate's
fail-closed branches (unlisted pipeline, empty allowlist, missing project
entry, no budget, over budget, require_approval, merge_pr), and the
single-objective-per-tick drain (pause/stop, allow-path dispatch,
no-auto_dispatch-block never dispatches).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services import autopilot_queue
from hivepilot.services.autopilot_policy import AutopilotPolicy

# ---------------------------------------------------------------------------
# Queue lifecycle
# ---------------------------------------------------------------------------


class TestQueueLifecycle:
    def test_enqueue_defaults_to_proposed(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "found stale docs")
        items = autopilot_queue.list_queue(tenant="default")
        assert len(items) == 1
        assert items[0].id == item_id
        assert items[0].state == "proposed"
        assert items[0].project == "acme-api"
        assert items[0].pipeline == "groomer"
        assert items[0].reason == "found stale docs"

    def test_enqueue_rejects_invalid_state(self) -> None:
        with pytest.raises(ValueError):
            autopilot_queue.enqueue("acme-api", "groomer", "x", state="not-a-real-state")

    def test_list_queue_filters_by_tenant(self) -> None:
        autopilot_queue.enqueue("acme-api", "groomer", "x", tenant="tenant-a")
        autopilot_queue.enqueue("acme-api", "groomer", "y", tenant="tenant-b")
        assert len(autopilot_queue.list_queue(tenant="tenant-a")) == 1
        assert len(autopilot_queue.list_queue(tenant="tenant-b")) == 1
        assert len(autopilot_queue.list_queue(tenant=None)) == 2

    def test_promote_advances_proposed_to_queued(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.promote(item_id)
        items = autopilot_queue.list_queue(tenant="default")
        assert items[0].state == "queued"

    def test_promote_is_noop_on_non_proposed_row(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.mark(item_id, "done")
        autopilot_queue.promote(item_id)
        items = autopilot_queue.list_queue(tenant="default")
        assert items[0].state == "done"  # unchanged -- promote only affects 'proposed'

    def test_veto_marks_vetoed(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.veto(item_id)
        items = autopilot_queue.list_queue(tenant="default")
        assert items[0].state == "vetoed"

    def test_mark_rejects_invalid_state(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        with pytest.raises(ValueError):
            autopilot_queue.mark(item_id, "nonsense")

    def test_mark_records_cost(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.mark(item_id, "done", cost_usd=1.23)
        items = autopilot_queue.list_queue(tenant="default")
        assert items[0].cost_usd == 1.23

    def test_next_dispatchable_prefers_queued_over_proposed(self) -> None:
        autopilot_queue.enqueue("acme-api", "proposed-pipeline", "x")
        queued_id = autopilot_queue.enqueue("acme-api", "queued-pipeline", "y")
        autopilot_queue.promote(queued_id)
        item = autopilot_queue.next_dispatchable(tenant="default")
        assert item is not None
        assert item.id == queued_id
        assert item.state == "queued"

    def test_next_dispatchable_falls_back_to_proposed(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        item = autopilot_queue.next_dispatchable(tenant="default")
        assert item is not None
        assert item.id == item_id

    def test_next_dispatchable_none_when_empty(self) -> None:
        assert autopilot_queue.next_dispatchable(tenant="default") is None

    def test_next_dispatchable_ignores_other_tenants(self) -> None:
        autopilot_queue.enqueue("acme-api", "groomer", "x", tenant="other-tenant")
        assert autopilot_queue.next_dispatchable(tenant="default") is None


# ---------------------------------------------------------------------------
# Pause / resume / stop kill switch
# ---------------------------------------------------------------------------


class TestControlFlags:
    def test_default_is_not_paused_or_stopped(self) -> None:
        assert autopilot_queue.is_paused(tenant="default") is False
        assert autopilot_queue.is_stopped(tenant="default") is False

    def test_pause_sets_paused(self) -> None:
        autopilot_queue.pause(tenant="default")
        assert autopilot_queue.is_paused(tenant="default") is True

    def test_resume_clears_pause_and_stop(self) -> None:
        autopilot_queue.pause(tenant="default")
        autopilot_queue.stop(tenant="default")
        autopilot_queue.resume(tenant="default")
        assert autopilot_queue.is_paused(tenant="default") is False
        assert autopilot_queue.is_stopped(tenant="default") is False

    def test_stop_is_reported_via_is_paused_too(self) -> None:
        autopilot_queue.stop(tenant="default")
        assert autopilot_queue.is_stopped(tenant="default") is True
        assert autopilot_queue.is_paused(tenant="default") is True

    def test_control_flags_are_tenant_scoped(self) -> None:
        autopilot_queue.pause(tenant="tenant-a")
        assert autopilot_queue.is_paused(tenant="tenant-a") is True
        assert autopilot_queue.is_paused(tenant="tenant-b") is False


# ---------------------------------------------------------------------------
# The gate — fail-closed coverage
# ---------------------------------------------------------------------------


def _allow_policy(**overrides: object) -> AutopilotPolicy:
    base = dict(auto_dispatch=["good-pipeline"], require_approval=False, budget_daily_usd=5.0)
    base.update(overrides)
    return AutopilotPolicy(**base)  # type: ignore[arg-type]


class TestAutopilotGate:
    def test_deny_missing_project_or_pipeline(self) -> None:
        decision = autopilot_queue.autopilot_gate(
            "", "good-pipeline", policies=_allow_policy(), budget=0.0
        )
        assert decision.allow is False

    def test_deny_unlisted_pipeline(self) -> None:
        decision = autopilot_queue.autopilot_gate(
            "acme-api", "sneaky-pipeline", policies=_allow_policy(), budget=0.0
        )
        assert decision.allow is False
        assert "allowlist" in decision.reason

    def test_deny_empty_allowlist(self) -> None:
        policy = _allow_policy(auto_dispatch=[])
        decision = autopilot_queue.autopilot_gate(
            "acme-api", "good-pipeline", policies=policy, budget=0.0
        )
        assert decision.allow is False

    def test_deny_missing_project_entry_resolves_to_empty_allowlist(self) -> None:
        # Simulates autopilot_policy.get_autopilot_policy() for a project with
        # no policies.yaml entry at all: empty allowlist, no budget.
        policy = AutopilotPolicy(auto_dispatch=[], require_approval=False, budget_daily_usd=None)
        decision = autopilot_queue.autopilot_gate(
            "unknown-project", "good-pipeline", policies=policy, budget=0.0
        )
        assert decision.allow is False

    def test_deny_no_budget_configured(self) -> None:
        policy = _allow_policy(budget_daily_usd=None)
        decision = autopilot_queue.autopilot_gate(
            "acme-api", "good-pipeline", policies=policy, budget=0.0
        )
        assert decision.allow is False
        assert "budget" in decision.reason

    def test_deny_zero_or_negative_budget_configured(self) -> None:
        policy = _allow_policy(budget_daily_usd=0.0)
        decision = autopilot_queue.autopilot_gate(
            "acme-api", "good-pipeline", policies=policy, budget=0.0
        )
        assert decision.allow is False

    def test_deny_over_budget(self) -> None:
        policy = _allow_policy(budget_daily_usd=5.0)
        decision = autopilot_queue.autopilot_gate(
            "acme-api", "good-pipeline", policies=policy, budget=5.0
        )
        assert decision.allow is False
        assert "budget" in decision.reason

    def test_deny_require_approval_true(self) -> None:
        policy = _allow_policy(require_approval=True)
        decision = autopilot_queue.autopilot_gate(
            "acme-api", "good-pipeline", policies=policy, budget=0.0
        )
        assert decision.allow is False
        assert "require_approval" in decision.reason

    def test_deny_merge_pr_true(self) -> None:
        with patch.object(autopilot_queue, "pipeline_would_auto_merge", return_value=True):
            decision = autopilot_queue.autopilot_gate(
                "acme-api", "good-pipeline", policies=_allow_policy(), budget=0.0
            )
        assert decision.allow is False
        assert "merge" in decision.reason.lower()

    def test_allow_when_every_condition_holds(self) -> None:
        with patch.object(autopilot_queue, "pipeline_would_auto_merge", return_value=False):
            decision = autopilot_queue.autopilot_gate(
                "acme-api", "good-pipeline", policies=_allow_policy(), budget=0.0
            )
        assert decision.allow is True

    def test_deny_none_policies(self) -> None:
        decision = autopilot_queue.autopilot_gate(
            "acme-api", "good-pipeline", policies=None, budget=0.0
        )
        assert decision.allow is False


# ---------------------------------------------------------------------------
# pipeline_would_auto_merge — raw YAML, no models.py dependency
# ---------------------------------------------------------------------------


class TestPipelineWouldAutoMerge:
    def test_unknown_pipeline_fails_closed_true(self) -> None:
        with patch.object(autopilot_queue, "_load_raw_yaml", return_value={}):
            assert autopilot_queue.pipeline_would_auto_merge("nonexistent") is True

    def test_merge_pr_true_detected(self) -> None:
        def fake_load(filename: object) -> dict:
            if "pipelines" in str(filename):
                return {"pipelines": {"p": {"stages": [{"name": "s", "task": "t"}]}}}
            return {"tasks": {"t": {"git": {"merge_pr": True}}}}

        with patch.object(autopilot_queue, "_load_raw_yaml", side_effect=fake_load):
            assert autopilot_queue.pipeline_would_auto_merge("p") is True

    def test_no_merge_pr_returns_false(self) -> None:
        def fake_load(filename: object) -> dict:
            if "pipelines" in str(filename):
                return {"pipelines": {"p": {"stages": [{"name": "s", "task": "t"}]}}}
            return {"tasks": {"t": {"git": {"merge_pr": False}}}}

        with patch.object(autopilot_queue, "_load_raw_yaml", side_effect=fake_load):
            assert autopilot_queue.pipeline_would_auto_merge("p") is False

    def test_missing_task_fails_closed_true(self) -> None:
        def fake_load(filename: object) -> dict:
            if "pipelines" in str(filename):
                return {"pipelines": {"p": {"stages": [{"name": "s", "task": "missing-task"}]}}}
            return {"tasks": {}}

        with patch.object(autopilot_queue, "_load_raw_yaml", side_effect=fake_load):
            assert autopilot_queue.pipeline_would_auto_merge("p") is True


# ---------------------------------------------------------------------------
# drain_one — single-objective-per-tick dispatch
# ---------------------------------------------------------------------------


class TestDrainOne:
    def test_no_items_returns_none(self) -> None:
        orchestrator = MagicMock()
        assert autopilot_queue.drain_one(orchestrator, tenant="default") is None
        orchestrator.run_pipeline.assert_not_called()

    def test_no_auto_dispatch_block_only_proposes_never_dispatches(self) -> None:
        autopilot_queue.enqueue("acme-api", "groomer", "x")
        orchestrator = MagicMock()
        with patch(
            "hivepilot.services.autopilot_queue.get_autopilot_policy",
            return_value=AutopilotPolicy(
                auto_dispatch=[], require_approval=False, budget_daily_usd=None
            ),
        ):
            result = autopilot_queue.drain_one(orchestrator, tenant="default")
        orchestrator.run_pipeline.assert_not_called()
        assert result is not None
        assert result.state == "proposed"

    def test_pause_halts_drain_within_one_tick(self) -> None:
        autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.pause(tenant="default")
        orchestrator = MagicMock()
        result = autopilot_queue.drain_one(orchestrator, tenant="default")
        assert result is None
        orchestrator.run_pipeline.assert_not_called()

    def test_stop_halts_drain_within_one_tick(self) -> None:
        autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.stop(tenant="default")
        orchestrator = MagicMock()
        result = autopilot_queue.drain_one(orchestrator, tenant="default")
        assert result is None
        orchestrator.run_pipeline.assert_not_called()

    def test_allow_path_dispatches_exactly_one_run_pipeline_and_records_cost(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.promote(item_id)
        orchestrator = MagicMock()
        policy = AutopilotPolicy(
            auto_dispatch=["groomer"], require_approval=False, budget_daily_usd=5.0
        )
        with (
            patch("hivepilot.services.autopilot_queue.get_autopilot_policy", return_value=policy),
            patch(
                "hivepilot.services.autopilot_queue.pipeline_would_auto_merge", return_value=False
            ),
            patch("hivepilot.services.autopilot_queue.spent_today_usd", side_effect=[0.0, 1.5]),
        ):
            result = autopilot_queue.drain_one(orchestrator, tenant="default")

        orchestrator.run_pipeline.assert_called_once()
        call_kwargs = orchestrator.run_pipeline.call_args.kwargs
        assert list(call_kwargs["project_names"]) == ["acme-api"]
        assert call_kwargs["pipeline_name"] == "groomer"

        assert result is not None
        items = autopilot_queue.list_queue(tenant="default")
        assert items[0].state == "done"
        assert items[0].cost_usd == 1.5

    def test_dispatch_failure_marks_blocked_not_done(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.promote(item_id)
        orchestrator = MagicMock()
        orchestrator.run_pipeline.side_effect = RuntimeError("boom")
        policy = AutopilotPolicy(
            auto_dispatch=["groomer"], require_approval=False, budget_daily_usd=5.0
        )
        with (
            patch("hivepilot.services.autopilot_queue.get_autopilot_policy", return_value=policy),
            patch(
                "hivepilot.services.autopilot_queue.pipeline_would_auto_merge", return_value=False
            ),
            patch("hivepilot.services.autopilot_queue.spent_today_usd", return_value=0.0),
        ):
            autopilot_queue.drain_one(orchestrator, tenant="default")

        items = autopilot_queue.list_queue(tenant="default")
        assert items[0].state == "blocked"

    def test_budget_check_failure_denies_not_raises(self) -> None:
        item_id = autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.promote(item_id)
        orchestrator = MagicMock()
        policy = AutopilotPolicy(
            auto_dispatch=["groomer"], require_approval=False, budget_daily_usd=5.0
        )
        with (
            patch("hivepilot.services.autopilot_queue.get_autopilot_policy", return_value=policy),
            patch(
                "hivepilot.services.autopilot_queue.spent_today_usd",
                side_effect=RuntimeError("analytics down"),
            ),
        ):
            result = autopilot_queue.drain_one(orchestrator, tenant="default")

        orchestrator.run_pipeline.assert_not_called()
        assert result is not None
        assert result.state == "queued"  # left exactly as-is, not silently advanced
