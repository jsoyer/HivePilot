"""
Tests for hivepilot.services.analytics_service (Phase 24a).

The `_isolate_state_db` fixture (conftest.py, autouse) redirects
`state_service.DB_PATH` to a per-test tmp file so these tests never touch a
real state.db.

Runs/steps/approvals are seeded via direct SQL so timestamps (and therefore
durations/percentiles) are fully controllable and deterministic.
"""

from __future__ import annotations

from hivepilot.services import analytics_service, db, state_service

# ---------------------------------------------------------------------------
# Seed helpers — direct SQL so started_at/finished_at/timestamp are exact
# ---------------------------------------------------------------------------


def _seed_run(
    project: str = "proj",
    task: str = "task",
    status: str = "success",
    tenant: str = "default",
    started_at: str = "2026-01-01 00:00:00",
    finished_at: str | None = None,
) -> int:
    state_service.init_db()
    with db.connect() as conn:
        return db.insert_returning_id(
            conn,
            "INSERT INTO runs (project, task, status, tenant, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, task, status, tenant, started_at, finished_at),
        )


def _seed_step(
    run_id: int,
    step: str,
    status: str,
    timestamp: str = "2026-01-01 00:00:00",
    provider: str | None = None,
    model: str | None = None,
) -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO steps (run_id, step, status, timestamp, provider, model) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (run_id, step, status, timestamp, provider, model),
        )


def _seed_step_with_usage(
    run_id: int,
    step: str,
    status: str,
    provider: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    timestamp: str = "2026-01-01 00:00:00",
) -> None:
    """Seed helper for Phase 24b.2b cost tests — writes the token/cost
    columns state_service.record_step() also accepts, via direct SQL for
    deterministic control (mirrors `_seed_step`)."""
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO steps "
                "(run_id, step, status, timestamp, provider, model, "
                "input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                run_id,
                step,
                status,
                timestamp,
                provider,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
            ),
        )


def _seed_approval(
    run_id: int,
    tenant: str = "default",
    project: str = "proj",
    task: str = "task",
    status: str = "approved",
    requested_at: str = "2026-01-01 00:00:00",
    approved_at: str | None = "2026-01-01 00:00:10",
) -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO approvals "
                "(run_id, project, task, status, tenant, requested_at, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            (run_id, project, task, status, tenant, requested_at, approved_at),
        )


# ---------------------------------------------------------------------------
# Canonical outcome mapping
# ---------------------------------------------------------------------------


class TestCanonicalOutcome:
    def test_success_maps_to_succeeded(self) -> None:
        assert analytics_service.canonical_outcome("success") == "succeeded"

    def test_complete_maps_to_succeeded(self) -> None:
        """RunStatus.COMPLETE == 'complete' must bucket with legacy 'success'."""
        assert analytics_service.canonical_outcome("complete") == "succeeded"

    def test_failed_maps_to_failed(self) -> None:
        assert analytics_service.canonical_outcome("failed") == "failed"

    def test_denied_maps_to_failed(self) -> None:
        assert analytics_service.canonical_outcome("denied") == "failed"

    def test_runstatus_failure_states_map_to_failed(self) -> None:
        for status in ("rate_limit", "auth_expired", "test_failure", "security_blocker"):
            assert analytics_service.canonical_outcome(status) == "failed"

    def test_deferred_maps_to_skipped(self) -> None:
        assert analytics_service.canonical_outcome("deferred") == "skipped"

    def test_running_maps_to_other(self) -> None:
        assert analytics_service.canonical_outcome("running") == "other"

    def test_unknown_status_maps_to_other(self) -> None:
        assert analytics_service.canonical_outcome("some_unknown_status") == "other"

    def test_none_maps_to_other(self) -> None:
        assert analytics_service.canonical_outcome(None) == "other"

    def test_case_insensitive(self) -> None:
        assert analytics_service.canonical_outcome("SUCCESS") == "succeeded"
        assert analytics_service.canonical_outcome("Complete") == "succeeded"


# ---------------------------------------------------------------------------
# run_summary
# ---------------------------------------------------------------------------


class TestRunSummary:
    def test_totals_and_outcome_counts(self) -> None:
        _seed_run(project="a", task="t1", status="success")
        _seed_run(project="a", task="t1", status="complete")
        _seed_run(project="a", task="t2", status="failed")
        _seed_run(project="b", task="t3", status="deferred")
        _seed_run(project="b", task="t3", status="running")

        result = analytics_service.run_summary(days=None)

        assert result["total"] == 5
        assert result["outcomes"] == {
            "succeeded": 2,
            "failed": 1,
            "skipped": 1,
            "other": 1,
        }

    def test_outcome_rates_sum_to_one(self) -> None:
        _seed_run(status="success")
        _seed_run(status="failed")
        _seed_run(status="failed")
        _seed_run(status="deferred")

        result = analytics_service.run_summary(days=None)
        rates = result["outcome_rates"]
        assert round(sum(rates.values()), 6) == 1.0
        assert rates["failed"] == 0.5

    def test_grouped_by_project(self) -> None:
        _seed_run(project="alpha", status="success")
        _seed_run(project="alpha", status="failed")
        _seed_run(project="beta", status="success")

        result = analytics_service.run_summary(days=None)
        assert result["by_project"]["alpha"]["total"] == 2
        assert result["by_project"]["alpha"]["outcomes"]["succeeded"] == 1
        assert result["by_project"]["alpha"]["outcomes"]["failed"] == 1
        assert result["by_project"]["beta"]["total"] == 1

    def test_grouped_by_task(self) -> None:
        _seed_run(task="build", status="success")
        _seed_run(task="build", status="success")
        _seed_run(task="deploy", status="failed")

        result = analytics_service.run_summary(days=None)
        assert result["by_task"]["build"]["total"] == 2
        assert result["by_task"]["deploy"]["outcomes"]["failed"] == 1

    def test_grouped_by_raw_status(self) -> None:
        _seed_run(status="success")
        _seed_run(status="complete")
        _seed_run(status="failed")

        result = analytics_service.run_summary(days=None)
        assert result["by_raw_status"]["success"] == 1
        assert result["by_raw_status"]["complete"] == 1
        assert result["by_raw_status"]["failed"] == 1

    def test_tenant_filter_excludes_other_tenants(self) -> None:
        _seed_run(status="success", tenant="acme")
        _seed_run(status="success", tenant="other")

        result = analytics_service.run_summary(tenant="acme", days=None)
        assert result["total"] == 1

    def test_project_and_task_filters(self) -> None:
        _seed_run(project="a", task="t1", status="success")
        _seed_run(project="a", task="t2", status="success")
        _seed_run(project="b", task="t1", status="success")

        result = analytics_service.run_summary(project="a", task="t1", days=None)
        assert result["total"] == 1

    def test_empty_db_returns_zero_total(self) -> None:
        result = analytics_service.run_summary(days=None)
        assert result["total"] == 0
        assert result["outcomes"] == {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "other": 0,
        }


# ---------------------------------------------------------------------------
# run_trends
# ---------------------------------------------------------------------------


class TestRunTrends:
    def test_day_bucketing(self) -> None:
        _seed_run(status="success", started_at="2026-01-01 09:00:00")
        _seed_run(status="failed", started_at="2026-01-01 15:00:00")
        _seed_run(status="success", started_at="2026-01-02 10:00:00")

        result = analytics_service.run_trends(days=None, bucket="day")
        series = {row["bucket"]: row for row in result["series"]}

        assert series["2026-01-01"]["total"] == 2
        assert series["2026-01-01"]["outcomes"]["succeeded"] == 1
        assert series["2026-01-01"]["outcomes"]["failed"] == 1
        assert series["2026-01-02"]["total"] == 1

    def test_week_bucketing_groups_same_iso_week(self) -> None:
        # 2026-01-05 (Mon) and 2026-01-07 (Wed) fall in the same ISO week.
        _seed_run(status="success", started_at="2026-01-05 00:00:00")
        _seed_run(status="success", started_at="2026-01-07 00:00:00")
        # 2026-01-12 (Mon) is the following ISO week.
        _seed_run(status="success", started_at="2026-01-12 00:00:00")

        result = analytics_service.run_trends(days=None, bucket="week")
        assert len(result["series"]) == 2
        totals = sorted(row["total"] for row in result["series"])
        assert totals == [1, 2]

    def test_invalid_bucket_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            analytics_service.run_trends(days=None, bucket="month")

    def test_series_sorted_ascending(self) -> None:
        _seed_run(status="success", started_at="2026-01-03 00:00:00")
        _seed_run(status="success", started_at="2026-01-01 00:00:00")
        _seed_run(status="success", started_at="2026-01-02 00:00:00")

        result = analytics_service.run_trends(days=None, bucket="day")
        buckets = [row["bucket"] for row in result["series"]]
        assert buckets == sorted(buckets)


# ---------------------------------------------------------------------------
# run_durations — percentile correctness
# ---------------------------------------------------------------------------


class TestRunDurations:
    def test_percentiles_exact_nearest_rank(self) -> None:
        """10 durations, 1..10 seconds. Nearest-rank method:
        rank = ceil(p/100 * n); index = rank - 1.
        p50 -> ceil(5.0)=5 -> idx4 -> 5
        p95 -> ceil(9.5)=10 -> idx9 -> 10
        p99 -> ceil(9.9)=10 -> idx9 -> 10
        """
        base = "2026-01-01 00:00:00"
        for i in range(1, 11):
            finished = f"2026-01-01 00:00:{i:02d}"
            _seed_run(status="success", started_at=base, finished_at=finished)

        result = analytics_service.run_durations(days=None)
        overall = result["overall"]
        assert overall["count"] == 10
        assert overall["min"] == 1.0
        assert overall["max"] == 10.0
        assert overall["avg"] == 5.5
        assert overall["p50"] == 5.0
        assert overall["p95"] == 10.0
        assert overall["p99"] == 10.0

    def test_unfinished_runs_excluded(self) -> None:
        _seed_run(status="running", started_at="2026-01-01 00:00:00", finished_at=None)
        _seed_run(
            status="success",
            started_at="2026-01-01 00:00:00",
            finished_at="2026-01-01 00:00:05",
        )

        result = analytics_service.run_durations(days=None)
        assert result["overall"]["count"] == 1

    def test_negative_delta_excluded_clock_skew(self) -> None:
        """finished_at BEFORE started_at (clock skew / bad data) must be
        excluded — never produce a negative duration or crash the percentile
        computation."""
        _seed_run(
            status="success",
            started_at="2026-01-01 00:00:10",
            finished_at="2026-01-01 00:00:00",
        )
        # One valid run alongside it, to prove the skewed row is dropped
        # rather than the whole dataset being discarded.
        _seed_run(
            status="success",
            started_at="2026-01-01 00:00:00",
            finished_at="2026-01-01 00:00:05",
        )

        result = analytics_service.run_durations(days=None)
        assert result["overall"]["count"] == 1
        assert result["overall"]["p50"] == 5.0
        assert result["overall"]["min"] >= 0.0

    def test_grouped_by_project(self) -> None:
        _seed_run(
            project="a",
            status="success",
            started_at="2026-01-01 00:00:00",
            finished_at="2026-01-01 00:00:02",
        )
        _seed_run(
            project="b",
            status="success",
            started_at="2026-01-01 00:00:00",
            finished_at="2026-01-01 00:00:20",
        )

        result = analytics_service.run_durations(days=None)
        assert result["by_project"]["a"]["p50"] == 2.0
        assert result["by_project"]["b"]["p50"] == 20.0

    def test_no_finished_runs_returns_zeroed_stats(self) -> None:
        result = analytics_service.run_durations(days=None)
        assert result["overall"]["count"] == 0
        assert result["overall"]["p50"] == 0.0


# ---------------------------------------------------------------------------
# step_failure_hotspots
# ---------------------------------------------------------------------------


class TestStepFailureHotspots:
    def test_ranked_by_failure_count(self) -> None:
        run1 = _seed_run(project="a", task="t1")
        run2 = _seed_run(project="a", task="t1")
        run3 = _seed_run(project="a", task="t1")

        # "deploy" step fails 3 times (across 3 runs)
        _seed_step(run1, "deploy", "failed")
        _seed_step(run2, "deploy", "failed")
        _seed_step(run3, "deploy", "failed")
        # "build" step fails once
        _seed_step(run1, "build", "failed")
        _seed_step(run2, "build", "success")
        _seed_step(run3, "build", "success")

        result = analytics_service.step_failure_hotspots(days=None)
        # First entry must be the highest-failure-count combo: deploy/failed x3
        assert result[0]["step"] == "deploy"
        assert result[0]["status"] == "failed"
        assert result[0]["count"] == 3

    def test_tenant_filter_via_run_join(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        _seed_step(run_acme, "deploy", "failed")
        _seed_step(run_other, "deploy", "failed")

        result = analytics_service.step_failure_hotspots(tenant="acme", days=None)
        total_count = sum(h["count"] for h in result)
        assert total_count == 1

    def test_success_steps_included_but_ranked_lower(self) -> None:
        run1 = _seed_run()
        _seed_step(run1, "lint", "success")

        result = analytics_service.step_failure_hotspots(days=None)
        assert any(h["step"] == "lint" and h["status"] == "success" for h in result)


# ---------------------------------------------------------------------------
# approval_latency
# ---------------------------------------------------------------------------


class TestApprovalLatency:
    def test_percentiles_exact(self) -> None:
        """4 latencies: 10, 20, 30, 40 seconds.
        p50 -> ceil(0.5*4)=2 -> idx1 -> 20
        p95 -> ceil(0.95*4)=4 -> idx3 -> 40
        """
        deltas = [10, 20, 30, 40]
        for i, delta in enumerate(deltas):
            run_id = _seed_run()
            requested = "2026-01-01 00:00:00"
            approved = f"2026-01-01 00:00:{delta:02d}"
            _seed_approval(run_id, requested_at=requested, approved_at=approved)

        result = analytics_service.approval_latency(days=None)
        assert result["count"] == 4
        assert result["p50"] == 20.0
        assert result["p95"] == 40.0

    def test_pending_approvals_excluded(self) -> None:
        run_id = _seed_run()
        _seed_approval(run_id, requested_at="2026-01-01 00:00:00", approved_at=None)

        result = analytics_service.approval_latency(days=None)
        assert result["count"] == 0

    def test_negative_delta_excluded_clock_skew(self) -> None:
        """approved_at BEFORE requested_at (clock skew / bad data) must be
        excluded — never produce a negative latency or crash the percentile
        computation."""
        run_skewed = _seed_run()
        _seed_approval(
            run_skewed,
            requested_at="2026-01-01 00:00:10",
            approved_at="2026-01-01 00:00:00",
        )
        # One valid approval alongside it, to prove the skewed row is
        # dropped rather than the whole dataset being discarded.
        run_valid = _seed_run()
        _seed_approval(
            run_valid,
            requested_at="2026-01-01 00:00:00",
            approved_at="2026-01-01 00:00:05",
        )

        result = analytics_service.approval_latency(days=None)
        assert result["count"] == 1
        assert result["p50"] == 5.0

    def test_tenant_filter(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        _seed_approval(
            run_acme,
            tenant="acme",
            requested_at="2026-01-01 00:00:00",
            approved_at="2026-01-01 00:00:10",
        )
        _seed_approval(
            run_other,
            tenant="other",
            requested_at="2026-01-01 00:00:00",
            approved_at="2026-01-01 00:00:20",
        )

        result = analytics_service.approval_latency(tenant="acme", days=None)
        assert result["count"] == 1
        assert result["p50"] == 10.0


# ---------------------------------------------------------------------------
# Time window resolution
# ---------------------------------------------------------------------------


class TestTimeWindow:
    def test_days_none_and_no_since_until_means_unbounded(self) -> None:
        _seed_run(status="success", started_at="2020-01-01 00:00:00")
        result = analytics_service.run_summary(days=None)
        assert result["total"] == 1

    def test_since_until_filters_precisely(self) -> None:
        _seed_run(status="success", started_at="2026-01-01 00:00:00")
        _seed_run(status="success", started_at="2026-06-01 00:00:00")

        result = analytics_service.run_summary(
            days=None, since="2026-05-01 00:00:00", until="2026-12-31 23:59:59"
        )
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# Phase 24b.1 — steps_by_provider / steps_by_model
# ---------------------------------------------------------------------------


class TestStepsByProvider:
    def test_grouped_counts_and_outcomes(self) -> None:
        run1 = _seed_run(project="a", task="t1")
        run2 = _seed_run(project="a", task="t1")
        run3 = _seed_run(project="a", task="t1")

        _seed_step(run1, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        _seed_step(run2, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        _seed_step(run3, "s1", "failed", provider="codex", model="gpt-5.5")

        result = analytics_service.steps_by_provider(days=None)
        by_key = {row["provider"]: row for row in result}

        assert by_key["claude"]["total"] == 2
        assert by_key["claude"]["outcomes"]["succeeded"] == 2
        assert by_key["codex"]["total"] == 1
        assert by_key["codex"]["outcomes"]["failed"] == 1

    def test_null_provider_grouped_as_unknown(self) -> None:
        """Steps recorded before this sprint (or with a genuinely unknown
        provider, e.g. a non-native-engine placeholder step) group under
        'unknown' rather than being dropped."""
        run1 = _seed_run()
        _seed_step(run1, "legacy-step", "success", provider=None, model=None)

        result = analytics_service.steps_by_provider(days=None)
        assert any(row["provider"] == "unknown" and row["total"] == 1 for row in result)

    def test_tenant_isolation_via_run_join(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        _seed_step(run_acme, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        _seed_step(run_other, "s1", "success", provider="claude", model="claude-sonnet-4-6")

        result = analytics_service.steps_by_provider(tenant="acme", days=None)
        total = sum(row["total"] for row in result)
        assert total == 1

    def test_project_and_task_filters(self) -> None:
        run_a = _seed_run(project="a", task="t1")
        run_b = _seed_run(project="b", task="t2")
        _seed_step(run_a, "s1", "success", provider="claude")
        _seed_step(run_b, "s1", "success", provider="codex")

        result = analytics_service.steps_by_provider(project="a", days=None)
        providers = {row["provider"] for row in result}
        assert providers == {"claude"}

    def test_outcome_rates_present(self) -> None:
        run1 = _seed_run()
        _seed_step(run1, "s1", "success", provider="claude")
        _seed_step(run1, "s2", "failed", provider="claude")

        result = analytics_service.steps_by_provider(days=None)
        row = next(r for r in result if r["provider"] == "claude")
        assert round(sum(row["outcome_rates"].values()), 6) == 1.0

    def test_empty_db_returns_empty_list(self) -> None:
        assert analytics_service.steps_by_provider(days=None) == []


class TestStepsByModel:
    def test_grouped_counts_and_outcomes(self) -> None:
        run1 = _seed_run()
        run2 = _seed_run()
        _seed_step(run1, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        _seed_step(run2, "s1", "success", provider="claude", model="claude-haiku-4-6")

        result = analytics_service.steps_by_model(days=None)
        models = {row["model"]: row["total"] for row in result}
        assert models["claude-sonnet-4-6"] == 1
        assert models["claude-haiku-4-6"] == 1

    def test_null_model_grouped_as_unknown(self) -> None:
        """A shell step: provider known, model genuinely unknown -> 'unknown'
        bucket, never dropped or invented."""
        run1 = _seed_run()
        _seed_step(run1, "shell-step", "success", provider="shell", model=None)

        result = analytics_service.steps_by_model(days=None)
        assert any(row["model"] == "unknown" and row["total"] == 1 for row in result)

    def test_tenant_isolation_via_run_join(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        _seed_step(run_acme, "s1", "success", model="claude-sonnet-4-6")
        _seed_step(run_other, "s1", "success", model="claude-sonnet-4-6")

        result = analytics_service.steps_by_model(tenant="acme", days=None)
        total = sum(row["total"] for row in result)
        assert total == 1


# ---------------------------------------------------------------------------
# Phase 24b.2b — cost_summary
# ---------------------------------------------------------------------------


class TestCostSummary:
    def test_self_reported_cost_preferred_over_estimate(self) -> None:
        """A self-reported cost_usd must win even though the tokens+model
        would estimate to a different (10.5) value via the price map."""
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=1.23,
        )

        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["cost_usd"] == 1.23
        assert result["overall"]["unpriced_steps"] == 0

    def test_tokens_only_priced_model_uses_estimate(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=None,
        )

        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["cost_usd"] == 10.5
        assert result["overall"]["unpriced_steps"] == 0

    def test_tokens_with_unpriced_model_counts_as_unpriced(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="acme-provider",
            model="totally-unlisted-model",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=None,
        )

        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["cost_usd"] == 0.0
        assert result["overall"]["unpriced_steps"] == 1
        # Token totals are still counted even though cost couldn't be priced.
        assert result["overall"]["input_tokens"] == 1_000_000
        assert result["overall"]["output_tokens"] == 500_000

    def test_no_usage_at_all_counts_as_unpriced(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(run1, "s1", "success", provider="shell", model=None)

        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["unpriced_steps"] == 1
        assert result["overall"]["cost_usd"] == 0.0
        assert result["overall"]["input_tokens"] == 0
        assert result["overall"]["output_tokens"] == 0

    def test_overall_total_steps_counts_every_step(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(run1, "s1", "success", provider="shell", model=None)
        _seed_step_with_usage(
            run1,
            "s2",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=1000,
            cost_usd=0.5,
        )

        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["total_steps"] == 2

    def test_grouped_by_provider_and_model(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=None,
        )
        _seed_step_with_usage(
            run1,
            "s2",
            "success",
            provider="codex",
            model="totally-unlisted-model",
            input_tokens=100,
            output_tokens=100,
            cost_usd=None,
        )

        result = analytics_service.cost_summary(days=None)
        by_provider = {row["provider"]: row for row in result["by_provider"]}
        assert by_provider["claude"]["cost_usd"] == 10.5
        assert by_provider["codex"]["unpriced_steps"] == 1

        by_model = {row["model"]: row for row in result["by_model"]}
        assert by_model["claude-sonnet-4-6"]["cost_usd"] == 10.5
        assert by_model["totally-unlisted-model"]["unpriced_steps"] == 1

    def test_null_provider_and_model_grouped_as_unknown(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(run1, "legacy-step", "success", provider=None, model=None)

        result = analytics_service.cost_summary(days=None)
        providers = {row["provider"] for row in result["by_provider"]}
        models = {row["model"] for row in result["by_model"]}
        assert "unknown" in providers
        assert "unknown" in models

    def test_tenant_isolation_via_run_join(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        _seed_step_with_usage(
            run_acme,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=None,
        )
        _seed_step_with_usage(
            run_other,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=None,
        )

        result = analytics_service.cost_summary(tenant="acme", days=None)
        assert result["overall"]["total_steps"] == 1
        assert result["overall"]["cost_usd"] == 10.5

    def test_project_and_task_filters(self) -> None:
        run_a = _seed_run(project="a", task="t1")
        run_b = _seed_run(project="b", task="t2")
        _seed_step_with_usage(run_a, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        _seed_step_with_usage(run_b, "s1", "success", provider="codex", model="gpt-5.5")

        result = analytics_service.cost_summary(project="a", days=None)
        providers = {row["provider"] for row in result["by_provider"]}
        assert providers == {"claude"}

    def test_empty_db_returns_zeroed_overall(self) -> None:
        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["total_steps"] == 0
        assert result["overall"]["cost_usd"] == 0.0
        assert result["overall"]["unpriced_steps"] == 0
        assert result["by_provider"] == []
        assert result["by_model"] == []
