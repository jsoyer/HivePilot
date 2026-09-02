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
    role: str | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
) -> None:
    """Seed helper for Phase 24b.2b cost tests — writes the token/cost
    columns state_service.record_step() also accepts, via direct SQL for
    deterministic control (mirrors `_seed_step`). `role` is additive
    (Mirador Agent Panels backend sprint). `cache_read_tokens`/
    `cache_creation_tokens` are additive (usage-capture-modelusage fix)."""
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO steps "
                "(run_id, step, status, timestamp, provider, model, "
                "input_tokens, output_tokens, cost_usd, role, "
                "cache_read_tokens, cache_creation_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
                role,
                cache_read_tokens,
                cache_creation_tokens,
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
# _attempt_success_rate (unit-level -- the actual metric-truth fix)
# ---------------------------------------------------------------------------


class TestAttemptSuccessRate:
    def test_excludes_skipped_and_other_from_denominator(self) -> None:
        counts = {"succeeded": 3, "failed": 1, "skipped": 2, "other": 5}
        # 3 / (3 + 1) == 0.75, NOT 3 / 11.
        assert analytics_service._attempt_success_rate(counts) == 0.75

    def test_none_when_zero_attempts_all_skipped(self) -> None:
        counts = {"succeeded": 0, "failed": 0, "skipped": 4, "other": 0}
        assert analytics_service._attempt_success_rate(counts) is None

    def test_none_when_all_counts_zero(self) -> None:
        counts = {"succeeded": 0, "failed": 0, "skipped": 0, "other": 0}
        assert analytics_service._attempt_success_rate(counts) is None

    def test_all_failed_is_zero_not_none(self) -> None:
        """Distinct from the "no attempts" case: an attempted-and-failed
        group has a real 0.0 success rate, not `None`."""
        counts = {"succeeded": 0, "failed": 3, "skipped": 0, "other": 0}
        assert analytics_service._attempt_success_rate(counts) == 0.0

    def test_all_succeeded_is_one(self) -> None:
        counts = {"succeeded": 5, "failed": 0, "skipped": 0, "other": 0}
        assert analytics_service._attempt_success_rate(counts) == 1.0


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

    def test_success_rate_excludes_skipped_from_denominator(self) -> None:
        """A group of 3 succeeded + 1 failed + 2 skipped must report
        success_rate = 3/4 = 0.75 (skipped excluded from the denominator),
        NOT 3/6 = 0.5 (the old bug: every bucket divided by `total`)."""
        _seed_run(status="success")
        _seed_run(status="success")
        _seed_run(status="success")
        _seed_run(status="failed")
        _seed_run(status="deferred")
        _seed_run(status="deferred")

        result = analytics_service.run_summary(days=None)

        assert result["total"] == 6
        assert result["outcomes"] == {
            "succeeded": 3,
            "failed": 1,
            "skipped": 2,
            "other": 0,
        }
        assert result["success_rate"] == 0.75
        # outcome_rates (the pre-existing, total-denominator rates) are
        # untouched by this fix -- succeeded/total stays 3/6.
        assert result["outcome_rates"]["succeeded"] == 0.5

    def test_success_rate_is_none_when_group_is_100_percent_skipped(self) -> None:
        """A 100%-SKIPPED group has zero attempts (succeeded + failed == 0):
        success_rate must be `None`, never `0.0` -- `0.0` would look
        identical to "every attempt failed", which is a different, and much
        more alarming, signal than "nothing was attempted at all"."""
        _seed_run(status="deferred")
        _seed_run(status="deferred")
        _seed_run(status="deferred")

        result = analytics_service.run_summary(days=None)

        assert result["total"] == 3
        assert result["outcomes"]["skipped"] == 3
        assert result["outcomes"]["failed"] == 0
        assert result["success_rate"] is None

    def test_success_rate_none_when_no_runs_at_all(self) -> None:
        result = analytics_service.run_summary(days=None)
        assert result["total"] == 0
        assert result["success_rate"] is None

    def test_grouped_by_project(self) -> None:
        _seed_run(project="alpha", status="success")
        _seed_run(project="alpha", status="failed")
        _seed_run(project="beta", status="success")

        result = analytics_service.run_summary(days=None)
        assert result["by_project"]["alpha"]["total"] == 2
        assert result["by_project"]["alpha"]["outcomes"]["succeeded"] == 1
        assert result["by_project"]["alpha"]["outcomes"]["failed"] == 1
        assert result["by_project"]["alpha"]["success_rate"] == 0.5
        assert result["by_project"]["beta"]["total"] == 1
        assert result["by_project"]["beta"]["success_rate"] == 1.0

    def test_grouped_by_project_100_percent_skipped_group_success_rate_is_none(self) -> None:
        """A single group (`by_project`) that's 100% SKIPPED must not report
        a misleading 0% success_rate -- it must be `None`, distinct from a
        group that actually attempted and failed every run."""
        _seed_run(project="alpha", status="success")
        _seed_run(project="quiet", status="deferred")
        _seed_run(project="quiet", status="deferred")

        result = analytics_service.run_summary(days=None)

        assert result["by_project"]["quiet"]["total"] == 2
        assert result["by_project"]["quiet"]["outcomes"]["skipped"] == 2
        assert result["by_project"]["quiet"]["success_rate"] is None
        # The healthy group is unaffected.
        assert result["by_project"]["alpha"]["success_rate"] == 1.0

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

    def test_success_rate_none_for_all_skipped_provider_group(self) -> None:
        """A provider whose steps are all skipped (`deferred`) must report
        `success_rate: None`, not a misleading `0.0`."""
        run1 = _seed_run()
        _seed_step(run1, "s1", "deferred", provider="codex")
        _seed_step(run1, "s2", "deferred", provider="codex")

        result = analytics_service.steps_by_provider(days=None)
        row = next(r for r in result if r["provider"] == "codex")
        assert row["outcomes"]["skipped"] == 2
        assert row["success_rate"] is None

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
        """A step that REACHED a model but recorded none -> 'unknown' bucket,
        never dropped or invented.

        Seeded with `provider="claude"`, not `"shell"`: a shell runner never
        invoked a model at all, so it is not a missing value but an
        inapplicable one, and it now leaves the model view entirely (see
        `TestModelViewExcludesNonModelSteps`)."""
        run1 = _seed_run()
        _seed_step(run1, "review", "success", provider="claude", model=None)

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

    def test_self_reported_zero_cost_is_not_treated_as_missing(self) -> None:
        """A self-reported cost_usd of exactly 0.0 must still win over the
        price-map estimate (which would be nonzero here) — `_step_cost` must
        check `cost_usd is not None`, not `if cost_usd:` (0.0 is falsy but a
        legitimate, present, self-reported value). Guards this precedence
        invariant against a future 'simplify the truthiness check' refactor."""
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=0.0,
        )

        result = analytics_service.cost_summary(days=None)
        # Must be exactly the self-reported 0.0, NOT the 10.5 the price map
        # would estimate for this model/token combination.
        assert result["overall"]["cost_usd"] == 0.0
        # A self-reported 0.0 is still a cost SIGNAL — this step is priced,
        # not unpriced.
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

    def test_a_shell_step_is_unpriceable_not_unpriced(self) -> None:
        """A shell runner never called a model, so no price is MISSING.

        Counting it as unpriced is what made the dashboard warn that the
        total was understated about 271 steps that could not have cost
        anything.
        """
        run1 = _seed_run()
        _seed_step_with_usage(run1, "s1", "success", provider="shell", model=None)

        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["unpriceable_steps"] == 1
        assert result["overall"]["unpriced_steps"] == 0
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

    def test_estimate_includes_cache_tokens_at_their_own_rate(self) -> None:
        """When cost isn't self-reported, the price-map estimate fallback
        must account for cache_read/cache_creation tokens too -- they are
        billed and must never be silently dropped from the estimate."""
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
            cache_read_tokens=1_000_000,
        )

        result = analytics_service.cost_summary(days=None)
        # claude-sonnet-4-6 default cache_read rate: 0.3 USD/Mtok.
        assert result["overall"]["cost_usd"] == 0.3
        assert result["overall"]["unpriced_steps"] == 0

    def test_cache_tokens_without_a_cache_rate_marks_step_unpriced(self, monkeypatch) -> None:
        """A model priced for base input/output but with no cache rate on
        record cannot be honestly estimated once cache tokens are involved
        -- must count as unpriced, never silently ignore the cache volume."""
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"no-cache-rate-model": {"input": 1.0, "output": 1.0}},
            raising=False,
        )
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="claude",
            model="no-cache-rate-model",
            input_tokens=100,
            output_tokens=100,
            cost_usd=None,
            cache_read_tokens=1_000_000,
        )

        result = analytics_service.cost_summary(days=None)
        assert result["overall"]["unpriced_steps"] == 1
        assert result["overall"]["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Pollen data endpoints sprint -- cost_summary by_project / by_role /
# unpriced_models
# ---------------------------------------------------------------------------


class TestCostSummaryByProjectAndRole:
    def test_grouped_by_project(self) -> None:
        run_a = _seed_run(project="proj-a")
        run_b = _seed_run(project="proj-b")
        _seed_step_with_usage(
            run_a,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cost_usd=None,
        )
        _seed_step_with_usage(
            run_b,
            "s1",
            "success",
            provider="codex",
            model="totally-unlisted-model",
            input_tokens=100,
            output_tokens=100,
            cost_usd=None,
        )

        result = analytics_service.cost_summary(days=None)
        by_project = {row["project"]: row for row in result["by_project"]}
        assert by_project["proj-a"]["cost_usd"] == 10.5
        assert by_project["proj-b"]["unpriced_steps"] == 1

    def test_by_project_tenant_isolation(self) -> None:
        run_acme = _seed_run(project="p", tenant="acme")
        run_other = _seed_run(project="p", tenant="other")
        _seed_step_with_usage(run_acme, "s1", "success", provider="claude", cost_usd=1.0)
        _seed_step_with_usage(run_other, "s1", "success", provider="claude", cost_usd=1.0)

        result = analytics_service.cost_summary(tenant="acme", days=None)
        assert result["by_project"] == [
            {
                "project": "p",
                "total_steps": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 1.0,
                "unpriced_steps": 0,
                "unpriceable_steps": 0,
                "unpriced_reasons": {},
            }
        ]

    def test_by_role_is_empty_list_and_documented_when_no_steps(self) -> None:
        """Mirador Agent Panels backend sprint: `steps.role` now exists, so
        `by_role` is a REAL (possibly empty) breakdown, never `None`."""
        result = analytics_service.cost_summary(days=None)
        assert result["by_role"] == []
        assert isinstance(result["by_role_note"], str)
        assert result["by_role_note"]

    def test_by_role_groups_by_real_role_and_null_becomes_unknown(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(run1, "s1", "success", cost_usd=1.0, role="developer")
        _seed_step_with_usage(run1, "s2", "success", cost_usd=2.0, role="developer")
        _seed_step_with_usage(run1, "s3", "success", cost_usd=3.0, role=None)

        result = analytics_service.cost_summary(days=None)
        by_role = {row["role"]: row for row in result["by_role"]}
        assert by_role["developer"]["cost_usd"] == 3.0
        assert by_role["developer"]["total_steps"] == 2
        assert by_role["unknown"]["cost_usd"] == 3.0
        assert by_role["unknown"]["total_steps"] == 1

    def test_by_role_tenant_isolation(self) -> None:
        run_acme = _seed_run(project="p", tenant="acme")
        run_other = _seed_run(project="p", tenant="other")
        _seed_step_with_usage(run_acme, "s1", "success", cost_usd=1.0, role="developer")
        _seed_step_with_usage(run_other, "s1", "success", cost_usd=1.0, role="developer")

        result = analytics_service.cost_summary(tenant="acme", days=None)
        assert result["by_role"] == [
            {
                "role": "developer",
                "total_steps": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 1.0,
                "unpriced_steps": 0,
                "unpriceable_steps": 0,
                "unpriced_reasons": {},
            }
        ]

    def test_unpriced_models_lists_models_with_any_unpriced_step(self) -> None:
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
        assert result["unpriced_models"] == ["totally-unlisted-model"]

    def test_unpriced_models_empty_when_no_steps(self) -> None:
        result = analytics_service.cost_summary(days=None)
        assert result["unpriced_models"] == []


# ---------------------------------------------------------------------------
# HP-81 — cost_whales (top-N steps by spend / prompt tokens)
# ---------------------------------------------------------------------------


class TestCostWhales:
    def test_empty_window_is_an_empty_list(self) -> None:
        result = analytics_service.cost_whales(days=None)
        assert result == {"whales": [], "limit": 20}

    def test_stargate_sized_step_ranks_first(self) -> None:
        """A $1.49 / 297k-token Claude Code call must not disappear into
        ``claude · 30d``. Chloe's Langfuse row is the fixture."""
        run1 = _seed_run(task="claude-code")
        _seed_step_with_usage(
            run1,
            "agent",
            "success",
            provider="claude",
            model="claude-opus-4-8",
            input_tokens=296_865,
            output_tokens=71,
            cost_usd=1.4861,
        )
        _seed_step_with_usage(
            run1,
            "cheap",
            "success",
            provider="claude",
            model="claude-haiku",
            input_tokens=1_200,
            output_tokens=80,
            cost_usd=0.012,
        )

        whales = analytics_service.cost_whales(days=None)["whales"]
        assert len(whales) == 2
        assert whales[0]["model"] == "claude-opus-4-8"
        assert whales[0]["cost_usd"] == 1.4861
        assert whales[0]["input_tokens"] == 296_865
        assert whales[0]["output_tokens"] == 71
        assert whales[0]["task"] == "claude-code"
        assert whales[0]["priced"] is True
        assert whales[1]["model"] == "claude-haiku"

    def test_envelope_only_never_selects_prompt_bodies(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "agent",
            "success",
            provider="claude",
            model="claude-opus-4-8",
            input_tokens=10_000,
            output_tokens=20,
            cost_usd=0.5,
        )
        whale = analytics_service.cost_whales(days=None)["whales"][0]
        assert set(whale.keys()) == {
            "step_id",
            "run_id",
            "project",
            "task",
            "step",
            "provider",
            "model",
            "timestamp",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "priced",
        }
        assert "detail" not in whale
        assert "prompt" not in whale
        assert "messages" not in whale

    def test_shell_and_skip_steps_are_not_whales(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "npm-test",
            "success",
            provider="shell",
            model=None,
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
        )
        _seed_step_with_usage(
            run1,
            "skip:docs",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=50_000,
            output_tokens=10,
            cost_usd=0.4,
        )
        _seed_step_with_usage(
            run1,
            "real",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=10,
            cost_usd=0.01,
        )
        whales = analytics_service.cost_whales(days=None)["whales"]
        assert [w["step"] for w in whales] == ["real"]

    def test_zero_cost_zero_token_steps_are_dropped(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "empty",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
        )
        assert analytics_service.cost_whales(days=None)["whales"] == []

    def test_same_cost_ranks_by_prompt_tokens(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "small",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000,
            output_tokens=10,
            cost_usd=0.5,
        )
        _seed_step_with_usage(
            run1,
            "huge",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=200_000,
            output_tokens=10,
            cost_usd=0.5,
        )
        whales = analytics_service.cost_whales(days=None)["whales"]
        assert [w["step"] for w in whales] == ["huge", "small"]

    def test_limit_caps_the_list(self) -> None:
        run1 = _seed_run()
        for i, cost in enumerate((0.3, 0.9, 0.1)):
            _seed_step_with_usage(
                run1,
                f"s{i}",
                "success",
                provider="claude",
                model="claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=10,
                cost_usd=cost,
            )
        result = analytics_service.cost_whales(days=None, limit=1)
        assert result["limit"] == 1
        assert len(result["whales"]) == 1
        assert result["whales"][0]["cost_usd"] == 0.9

    def test_tenant_isolation(self) -> None:
        acme = _seed_run(tenant="acme")
        other = _seed_run(tenant="other")
        _seed_step_with_usage(
            acme,
            "a",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=10,
            cost_usd=1.0,
        )
        _seed_step_with_usage(
            other,
            "o",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=10,
            cost_usd=9.0,
        )
        whales = analytics_service.cost_whales(tenant="acme", days=None)["whales"]
        assert len(whales) == 1
        assert whales[0]["cost_usd"] == 1.0


# ---------------------------------------------------------------------------
# Pollen data endpoints sprint -- models_summary (GET /v1/models)
# ---------------------------------------------------------------------------


class TestModelsSummary:
    def test_empty_db_returns_empty_models_and_zero_overall(self) -> None:
        result = analytics_service.models_summary(days=None)
        assert result["models"] == []
        assert result["overall"]["cost_usd"] == 0.0
        assert result["overall"]["cost_per_successful_run"] is None
        assert result["latency_available"] is False
        assert isinstance(result["latency_note"], str) and result["latency_note"]

    def test_per_model_rollup_fields(self) -> None:
        run1 = _seed_run(status="success")
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
        result = analytics_service.models_summary(days=None)
        assert len(result["models"]) == 1
        row = result["models"][0]
        assert row["model"] == "claude-sonnet-4-6"
        assert row["step_count"] == 1
        assert row["input_tokens"] == 1_000_000
        assert row["output_tokens"] == 500_000
        assert row["cost_usd"] == 10.5
        assert row["unpriced_steps"] == 0
        assert row["success_rate"] == 1.0
        assert row["share_of_spend"] == 1.0

    def test_share_of_spend_across_models(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(
            run1,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=0,
            cost_usd=3.0,
        )
        _seed_step_with_usage(
            run1,
            "s2",
            "success",
            provider="claude",
            model="claude-opus-4-6",
            input_tokens=1_000_000,
            output_tokens=0,
            cost_usd=1.0,
        )
        result = analytics_service.models_summary(days=None)
        by_model = {r["model"]: r for r in result["models"]}
        assert by_model["claude-sonnet-4-6"]["share_of_spend"] == 0.75
        assert by_model["claude-opus-4-6"]["share_of_spend"] == 0.25

    def test_success_rate_excludes_skipped_from_denominator(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(run1, "s1", "success", provider="claude", model="m")
        _seed_step_with_usage(run1, "s2", "failed", provider="claude", model="m")
        _seed_step_with_usage(run1, "s3", "deferred", provider="claude", model="m")
        result = analytics_service.models_summary(days=None)
        row = result["models"][0]
        assert row["success_rate"] == 0.5

    def test_cost_per_successful_run(self) -> None:
        run1 = _seed_run(status="success")
        run2 = _seed_run(status="failed")
        _seed_step_with_usage(run1, "s1", "success", provider="claude", model="m", cost_usd=4.0)
        _seed_step_with_usage(run2, "s1", "failed", provider="claude", model="m", cost_usd=2.0)
        result = analytics_service.models_summary(days=None)
        assert result["overall"]["succeeded_runs"] == 1
        assert result["overall"]["cost_per_successful_run"] == 6.0

    def test_null_model_groups_as_unknown(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(run1, "s1", "success", provider="shell", model=None)
        result = analytics_service.models_summary(days=None)
        assert result["models"][0]["model"] == "unknown"

    def test_tenant_isolation(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        _seed_step_with_usage(run_acme, "s1", "success", provider="claude", model="m", cost_usd=1.0)
        _seed_step_with_usage(
            run_other, "s1", "success", provider="claude", model="m", cost_usd=1.0
        )
        result = analytics_service.models_summary(tenant="acme", days=None)
        assert result["overall"]["total_steps"] == 1

    def test_models_sorted_by_cost_descending(self) -> None:
        run1 = _seed_run()
        _seed_step_with_usage(run1, "s1", "success", provider="claude", model="cheap", cost_usd=1.0)
        _seed_step_with_usage(
            run1, "s2", "success", provider="claude", model="pricey", cost_usd=9.0
        )
        result = analytics_service.models_summary(days=None)
        assert [row["model"] for row in result["models"]] == ["pricey", "cheap"]


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint -- agents_summary (GET /v1/agents)
# ---------------------------------------------------------------------------


class TestAgentsSummary:
    def test_empty_db_returns_full_roster_all_unattributed(self) -> None:
        """Every role in the current roster (roles.yaml) must appear, even
        with zero activity -- honest 'no data yet', never dropped."""
        result = analytics_service.agents_summary(days=None)
        names = {a["name"] for a in result["agents"]}
        assert "developer" in names
        assert "reviewer" in names
        for agent in result["agents"]:
            assert agent["attributed"] is False
            assert agent["step_count"] == 0
            assert agent["run_count"] == 0
            assert agent["success_rate"] is None
            assert agent["cost_usd"] == 0.0
            assert agent["last_active"] is None
        assert result["unknown"]["step_count"] == 0
        assert isinstance(result["note"], str) and result["note"]

    def test_attributed_role_reflects_real_activity(self) -> None:
        run1 = _seed_run(status="success")
        _seed_step_with_usage(run1, "s1", "success", cost_usd=2.0, role="developer")
        result = analytics_service.agents_summary(days=None)
        by_name = {a["name"]: a for a in result["agents"]}
        dev = by_name["developer"]
        assert dev["attributed"] is True
        assert dev["step_count"] == 1
        assert dev["run_count"] == 1
        assert dev["cost_usd"] == 2.0
        assert dev["success_rate"] == 1.0
        assert dev["last_active"] == "2026-01-01 00:00:00"
        assert dev["display_name"] == "Gustave"
        assert dev["title"] == "Developer"
        # A role with no attributed steps stays honestly empty.
        reviewer = by_name["reviewer"]
        assert reviewer["attributed"] is False
        assert reviewer["success_rate"] is None

    def test_success_rate_none_when_attributed_but_no_attempts(self) -> None:
        """A role with real steps but none succeeded/failed (e.g. all
        'running') must still report success_rate=None, never a fabricated
        0%/100%."""
        run1 = _seed_run(status="running")
        _seed_step_with_usage(run1, "s1", "deferred", role="developer")
        result = analytics_service.agents_summary(days=None)
        dev = next(a for a in result["agents"] if a["name"] == "developer")
        assert dev["attributed"] is True
        assert dev["step_count"] == 1
        assert dev["success_rate"] is None

    def test_null_role_grouped_under_unknown_not_dropped_not_guessed(self) -> None:
        run1 = _seed_run(status="success")
        _seed_step_with_usage(run1, "s1", "success", cost_usd=5.0, role=None)
        result = analytics_service.agents_summary(days=None)
        assert result["unknown"]["step_count"] == 1
        assert result["unknown"]["cost_usd"] == 5.0
        # Never silently merged into any named role's stats.
        for agent in result["agents"]:
            assert agent["step_count"] == 0

    def test_role_observed_but_not_in_current_roster_is_still_surfaced(self) -> None:
        """A role name present in the data but no longer in roles.yaml
        (e.g. the roster changed) must still be reported honestly, not
        silently dropped -- just without a display_name/title."""
        run1 = _seed_run(status="success")
        _seed_step_with_usage(run1, "s1", "success", cost_usd=1.0, role="ghost-role")
        result = analytics_service.agents_summary(days=None)
        ghost = next(a for a in result["agents"] if a["name"] == "ghost-role")
        assert ghost["attributed"] is True
        assert ghost["step_count"] == 1
        assert ghost["display_name"] is None
        assert ghost["title"] is None

    def test_tenant_isolation(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        _seed_step_with_usage(run_acme, "s1", "success", cost_usd=1.0, role="developer")
        _seed_step_with_usage(run_other, "s1", "success", cost_usd=1.0, role="developer")
        result = analytics_service.agents_summary(tenant="acme", days=None)
        dev = next(a for a in result["agents"] if a["name"] == "developer")
        assert dev["step_count"] == 1
        assert dev["cost_usd"] == 1.0

    def test_no_latency_field_ever_fabricated(self) -> None:
        result = analytics_service.agents_summary(days=None)
        for agent in result["agents"]:
            assert "latency" not in agent
            assert "p50" not in agent
            assert "p95" not in agent

    # -- unknown-bucket breakdown -------------------------------------------
    #
    # The bucket was a single undifferentiated number described in the UI as
    # "recorded before per-role attribution existed". On real data that
    # explanation was wrong for every row in it: 210 of 245 were `shell`
    # steps that cannot have a role at all, 16 were skips that never ran, and
    # 19 were model invocations carrying $4.81 that genuinely should have
    # been attributed and were not. Only the last group is a defect, and
    # lumping the three together hid it behind a number that looked like
    # legacy noise.

    def test_unknown_bucket_splits_by_cause(self) -> None:
        run = _seed_run(status="success")
        # No LLM involved -- a shell step has no role by construction.
        _seed_step_with_usage(run, "signals", "success", provider="shell")
        # Never ran, so it invoked nothing.
        _seed_step_with_usage(run, "skip:Design Spec", "skipped", provider=None)
        # A model ran and produced cost, with no role recorded. The defect.
        _seed_step_with_usage(run, "propose", "success", provider="claude", cost_usd=2.5)

        breakdown = analytics_service.agents_summary(days=None)["unknown"]["breakdown"]

        assert breakdown["no_model"]["step_count"] == 1
        assert breakdown["skipped"]["step_count"] == 1
        assert breakdown["attribution_gap"]["step_count"] == 1

    def test_only_the_attribution_gap_carries_cost(self) -> None:
        """Cost in the gap is spend missing from every per-agent figure.

        Shell steps and skips cost nothing, so a non-zero cost anywhere but
        the gap would mean the classifier put a real model invocation in a
        bucket the UI describes as harmless.
        """
        run = _seed_run(status="success")
        _seed_step_with_usage(run, "signals", "success", provider="shell", cost_usd=0.0)
        _seed_step_with_usage(run, "skip:x", "skipped", provider=None)
        _seed_step_with_usage(run, "propose", "success", provider="claude", cost_usd=4.75)

        breakdown = analytics_service.agents_summary(days=None)["unknown"]["breakdown"]

        assert breakdown["attribution_gap"]["cost_usd"] == 4.75
        assert breakdown["no_model"]["cost_usd"] == 0.0
        assert breakdown["skipped"]["cost_usd"] == 0.0

    def test_a_shell_step_is_never_reported_as_an_attribution_gap(self) -> None:
        """The gap count is the number the operator is asked to act on.

        Counting 210 roleless `shell` steps as gaps would bury the 19 real
        ones and make the figure impossible to act on -- the same failure as
        the undifferentiated bucket, one level down.
        """
        run = _seed_run(status="success")
        for index in range(5):
            _seed_step_with_usage(run, f"signals-{index}", "success", provider="shell")

        breakdown = analytics_service.agents_summary(days=None)["unknown"]["breakdown"]

        assert breakdown["no_model"]["step_count"] == 5
        assert breakdown["attribution_gap"]["step_count"] == 0

    def test_breakdown_accounts_for_every_row_in_the_bucket(self) -> None:
        """A breakdown that drops rows would understate the gap silently."""
        run = _seed_run(status="success")
        _seed_step_with_usage(run, "signals", "success", provider="shell")
        _seed_step_with_usage(run, "skip:a", "skipped", provider=None)
        _seed_step_with_usage(run, "propose", "success", provider="claude", cost_usd=1.0)
        # A NULL provider that is NOT a skip: a telemetry gap, not a
        # known-harmless row, so it must land in the gap rather than vanish.
        _seed_step_with_usage(run, "ceo intake", "success", provider=None, cost_usd=0.5)

        unknown = analytics_service.agents_summary(days=None)["unknown"]
        breakdown = unknown["breakdown"]

        assert sum(part["step_count"] for part in breakdown.values()) == unknown["step_count"]
        assert breakdown["attribution_gap"]["step_count"] == 2

    def test_attributed_steps_never_enter_the_breakdown(self) -> None:
        run = _seed_run(status="success")
        _seed_step_with_usage(
            run, "build", "success", provider="claude", cost_usd=9.0, role="developer"
        )

        unknown = analytics_service.agents_summary(days=None)["unknown"]

        assert unknown["step_count"] == 0
        assert all(part["step_count"] == 0 for part in unknown["breakdown"].values())


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint -- verdicts_summary (GET /v1/verdicts)
# ---------------------------------------------------------------------------


class TestVerdictsSummary:
    def test_empty_db_returns_empty(self) -> None:
        result = analytics_service.verdicts_summary()
        assert result["verdicts"] == []
        assert result["by_role"] == {}

    def test_groups_by_role_with_decision_and_kind_counts(self) -> None:
        run_id = _seed_run(tenant="acme")
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="approve",
            confidence=0.9,
        )
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="reject",
            confidence=0.4,
        )
        result = analytics_service.verdicts_summary(tenant="acme")
        assert len(result["verdicts"]) == 2
        by_role = result["by_role"]["reviewer"]
        assert by_role["total"] == 2
        assert by_role["decision_counts"] == {"approve": 1, "reject": 1}
        assert by_role["kind_counts"] == {"review": 2}

    def test_tenant_isolation(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        state_service.record_verdict(
            run_id=run_acme,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="approve",
            confidence=0.9,
        )
        state_service.record_verdict(
            run_id=run_other,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="reject",
            confidence=0.9,
        )
        result = analytics_service.verdicts_summary(tenant="acme")
        assert len(result["verdicts"]) == 1
        assert result["verdicts"][0]["decision"] == "approve"

    def test_role_filter(self) -> None:
        run_id = _seed_run(tenant="acme")
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="approve",
            confidence=0.9,
        )
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="developer",
            kind="debate",
            decision="approve",
            confidence=0.9,
        )
        result = analytics_service.verdicts_summary(tenant="acme", role="reviewer")
        assert len(result["verdicts"]) == 1
        assert result["by_role"] == {
            "reviewer": {
                "total": 1,
                "decision_counts": {"approve": 1},
                "kind_counts": {"review": 1},
            }
        }


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint -- lessons_summary (GET /v1/lessons)
# ---------------------------------------------------------------------------


class TestLessonsSummary:
    def test_empty_db_returns_empty(self) -> None:
        result = analytics_service.lessons_summary()
        assert result["lessons"] == []
        assert result["by_role"] == {}

    def test_groups_by_role_with_validated_and_avg_score(self) -> None:
        run_id = _seed_run(tenant="acme")
        state_service.record_lesson(
            run_id=run_id,
            project="p",
            role="developer",
            task="t",
            text="lesson 1",
            score=0.8,
            confidence=0.5,
            category="test",
            validated=True,
        )
        state_service.record_lesson(
            run_id=run_id,
            project="p",
            role="developer",
            task="t",
            text="lesson 2",
            score=None,
            confidence=None,
            category="test",
            validated=False,
        )
        result = analytics_service.lessons_summary(tenant="acme")
        assert len(result["lessons"]) == 2
        by_role = result["by_role"]["developer"]
        assert by_role["total"] == 2
        assert by_role["validated"] == 1
        assert by_role["avg_score"] == 0.8

    def test_tenant_isolation(self) -> None:
        run_acme = _seed_run(tenant="acme")
        run_other = _seed_run(tenant="other")
        state_service.record_lesson(
            run_id=run_acme,
            project="p",
            role="developer",
            task="t",
            text="acme lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        state_service.record_lesson(
            run_id=run_other,
            project="p",
            role="developer",
            task="t",
            text="other lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        result = analytics_service.lessons_summary(tenant="acme")
        assert len(result["lessons"]) == 1
        assert result["lessons"][0]["text"] == "acme lesson"

    def test_role_filter(self) -> None:
        run_id = _seed_run(tenant="acme")
        state_service.record_lesson(
            run_id=run_id,
            project="p",
            role="developer",
            task="t",
            text="dev lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        state_service.record_lesson(
            run_id=run_id,
            project="p",
            role="reviewer",
            task="t",
            text="reviewer lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        result = analytics_service.lessons_summary(tenant="acme", role="reviewer")
        assert len(result["lessons"]) == 1
        assert result["lessons"][0]["role"] == "reviewer"


class TestModelViewExcludesNonModelSteps:
    """A shell command is not a model, and a skipped stage is not a run.

    Pooling them produced an `unknown` pseudo-model with 234 steps and a 9%
    "success rate" that belonged to neither a model nor an agent — while the
    real telemetry gap (9 claude steps with no model recorded) was invisible
    inside it.
    """

    def test_shell_steps_do_not_appear_as_a_model(self) -> None:
        run = _seed_run()
        _seed_step(run, "signals", "failed", provider="shell", model=None)
        _seed_step(run, "review", "success", provider="claude", model="claude-opus-5")

        models = {row["model"] for row in analytics_service.steps_by_model(days=None)}
        assert "claude-opus-5" in models
        assert "unknown" not in models

    def test_skipped_stages_do_not_appear_as_a_model(self) -> None:
        run = _seed_run()
        _seed_step(run, "skip:Design Spec", "skipped", provider=None, model=None)
        _seed_step(run, "review", "success", provider="claude", model="claude-opus-5")

        result = analytics_service.steps_by_model(days=None)
        assert {row["model"] for row in result} == {"claude-opus-5"}

    def test_a_real_telemetry_gap_stays_visible(self) -> None:
        """claude ran but recorded no model — that must NOT be filtered away."""
        run = _seed_run()
        _seed_step(run, "review", "success", provider="claude", model=None)

        result = analytics_service.steps_by_model(days=None)
        assert any(row["model"] == "unknown" and row["total"] == 1 for row in result)


class TestUnpricedReasonBlamesTheRightSubsystem:
    """The banner said "no pricing data on record" and pointed at the price
    map. On the reference deployment it was wrong for every model it named:
    `opus`/`sonnet`/`haiku` are IN the map, and their unpriced steps were
    exactly the steps that recorded no tokens.
    """

    def test_a_known_model_with_no_tokens_blames_the_usage_capture(self) -> None:
        run = _seed_run()
        _seed_step_with_usage(run, "s1", "success", provider="claude", model="sonnet")

        overall = analytics_service.cost_summary(days=None)["overall"]
        assert overall["unpriced_steps"] == 1
        assert overall["unpriced_reasons"] == {"no_usage_captured": 1}

    def test_an_unknown_model_blames_the_price_map(self) -> None:
        run = _seed_run()
        _seed_step_with_usage(run, "s1", "success", provider="claude", model="model-not-in-any-map")

        overall = analytics_service.cost_summary(days=None)["overall"]
        assert overall["unpriced_reasons"] == {"no_price_for_model": 1}

    def test_a_step_with_no_model_at_all_is_its_own_reason(self) -> None:
        run = _seed_run()
        _seed_step_with_usage(run, "s1", "success", provider="claude", model=None)

        overall = analytics_service.cost_summary(days=None)["overall"]
        assert overall["unpriced_reasons"] == {"no_model_recorded": 1}

    def test_a_shell_step_produces_no_reason_at_all(self) -> None:
        """It is unpriceable, not unpriced — it belongs to neither cause."""
        run = _seed_run()
        _seed_step_with_usage(run, "s1", "success", provider="shell", model=None)

        overall = analytics_service.cost_summary(days=None)["overall"]
        assert overall["unpriced_reasons"] == {}
        assert overall["unpriceable_steps"] == 1
