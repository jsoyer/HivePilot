"""Tests for `hivepilot.services.memory_service` — the memory-quality
instrumentation subsystem backing Pollen's Memory > Quality view.

The `_isolate_state_db` fixture (autouse, defined in conftest.py) redirects
`state_service.DB_PATH` to a per-test tmp file, which `memory_service` reuses
via `hivepilot.services.db` — so these tests never touch the real
``./state.db``.
"""

from __future__ import annotations

import pytest

from hivepilot.services import memory_service

# ---------------------------------------------------------------------------
# record_* — inserts
# ---------------------------------------------------------------------------


class TestRecordEvents:
    def test_record_search_is_queryable(self):
        memory_service.record_search(namespace="ns", query="q", result_count=3, actor="agent-1")
        journal = memory_service.activity_journal(tenant="default", limit=10)
        assert len(journal) == 1
        assert journal[0]["op"] == "search"
        assert journal[0]["namespace"] == "ns"
        assert journal[0]["query_or_key"] == "q"
        assert journal[0]["result_count"] == 3
        assert journal[0]["actor"] == "agent-1"

    def test_record_read_is_queryable(self):
        memory_service.record_read(namespace="ns", key="k", found=True, actor="agent-1")
        journal = memory_service.activity_journal(tenant="default", limit=10)
        assert len(journal) == 1
        assert journal[0]["op"] == "read"
        assert journal[0]["found"] is True

    def test_record_store_is_queryable(self):
        memory_service.record_store(namespace="ns", key="k", actor="agent-1")
        journal = memory_service.activity_journal(tenant="default", limit=10)
        assert len(journal) == 1
        assert journal[0]["op"] == "store"

    def test_record_evaluation_is_queryable(self):
        memory_service.record_evaluation(
            namespace="ns", useful=True, actor="human-1", ref_key="k", note="great"
        )
        evals = memory_service.recent_evaluations(tenant="default", limit=10)
        assert len(evals) == 1
        assert evals[0]["namespace"] == "ns"
        assert evals[0]["useful"] is True
        assert evals[0]["note"] == "great"
        assert evals[0]["actor"] == "human-1"


# ---------------------------------------------------------------------------
# reality_summary — rates, incl. empty -> zeros, no divide-by-zero
# ---------------------------------------------------------------------------


class TestRealitySummary:
    def test_empty_is_all_zeros_no_crash(self):
        summary = memory_service.reality_summary(tenant="default", days=30)
        assert summary == {
            "search_success_rate": 0.0,
            "total_searches": 0,
            "no_result_count": 0,
            "avg_freshness_seconds": 0.0,
            "declared_reliability": 0.0,
            "total_evaluations": 0,
        }

    def test_search_success_rate_computed_correctly(self):
        memory_service.record_search(namespace="ns", query="a", result_count=2, actor="x")
        memory_service.record_search(namespace="ns", query="b", result_count=0, actor="x")
        memory_service.record_search(namespace="ns", query="c", result_count=5, actor="x")
        summary = memory_service.reality_summary(tenant="default", days=30)
        assert summary["total_searches"] == 3
        assert summary["no_result_count"] == 1
        assert summary["search_success_rate"] == pytest.approx(2 / 3, rel=1e-4)

    def test_avg_freshness_seconds(self):
        memory_service.record_search(
            namespace="ns", query="a", result_count=1, actor="x", freshness_seconds=10.0
        )
        memory_service.record_search(
            namespace="ns", query="b", result_count=1, actor="x", freshness_seconds=20.0
        )
        summary = memory_service.reality_summary(tenant="default", days=30)
        assert summary["avg_freshness_seconds"] == pytest.approx(15.0)

    def test_declared_reliability_computed_correctly(self):
        memory_service.record_evaluation(namespace="ns", useful=True, actor="h")
        memory_service.record_evaluation(namespace="ns", useful=True, actor="h")
        memory_service.record_evaluation(namespace="ns", useful=False, actor="h")
        summary = memory_service.reality_summary(tenant="default", days=30)
        assert summary["total_evaluations"] == 3
        assert summary["declared_reliability"] == pytest.approx(2 / 3, rel=1e-4)

    def test_admin_unscoped_tenant_none_sees_all(self):
        memory_service.record_search(
            namespace="ns", query="a", result_count=1, actor="x", tenant="acme"
        )
        memory_service.record_search(
            namespace="ns", query="b", result_count=1, actor="x", tenant="other"
        )
        summary = memory_service.reality_summary(tenant=None, days=30)
        assert summary["total_searches"] == 2


# ---------------------------------------------------------------------------
# gaps_by_namespace
# ---------------------------------------------------------------------------


class TestGapsByNamespace:
    def test_groups_no_result_searches_by_namespace(self):
        memory_service.record_search(namespace="ns-a", query="q1", result_count=0, actor="x")
        memory_service.record_search(namespace="ns-a", query="q1", result_count=0, actor="x")
        memory_service.record_search(namespace="ns-a", query="q2", result_count=0, actor="x")
        memory_service.record_search(namespace="ns-b", query="q3", result_count=0, actor="x")
        # A successful search must never count as a gap.
        memory_service.record_search(namespace="ns-a", query="q4", result_count=5, actor="x")

        gaps = memory_service.gaps_by_namespace(tenant="default", days=30)
        by_ns = {g["namespace"]: g for g in gaps}
        assert by_ns["ns-a"]["no_result_count"] == 3
        assert "q1" in by_ns["ns-a"]["top_queries"]
        assert by_ns["ns-b"]["no_result_count"] == 1

    def test_empty_returns_empty_list(self):
        assert memory_service.gaps_by_namespace(tenant="default", days=30) == []


# ---------------------------------------------------------------------------
# recent_evaluations / activity_journal — recency + tenant scoping
# ---------------------------------------------------------------------------


class TestRecentAndJournal:
    def test_recent_evaluations_empty_is_empty_list(self):
        assert memory_service.recent_evaluations(tenant="default", limit=10) == []

    def test_activity_journal_empty_is_empty_list(self):
        assert memory_service.activity_journal(tenant="default", limit=10) == []

    def test_activity_journal_respects_limit(self):
        for i in range(5):
            memory_service.record_store(namespace="ns", key=f"k{i}", actor="x")
        journal = memory_service.activity_journal(tenant="default", limit=2)
        assert len(journal) == 2


# ---------------------------------------------------------------------------
# Tenant isolation — the security-critical invariant.
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_reality_summary_never_leaks_across_tenants(self):
        memory_service.record_search(
            namespace="ns", query="a", result_count=1, actor="x", tenant="acme"
        )
        memory_service.record_evaluation(namespace="ns", useful=True, actor="h", tenant="acme")

        summary_other = memory_service.reality_summary(tenant="other", days=30)
        assert summary_other["total_searches"] == 0
        assert summary_other["total_evaluations"] == 0

        summary_acme = memory_service.reality_summary(tenant="acme", days=30)
        assert summary_acme["total_searches"] == 1
        assert summary_acme["total_evaluations"] == 1

    def test_gaps_never_leak_across_tenants(self):
        memory_service.record_search(
            namespace="ns", query="a", result_count=0, actor="x", tenant="acme"
        )
        assert memory_service.gaps_by_namespace(tenant="other", days=30) == []
        assert len(memory_service.gaps_by_namespace(tenant="acme", days=30)) == 1

    def test_journal_never_leaks_across_tenants(self):
        memory_service.record_store(namespace="ns", key="k", actor="x", tenant="acme")
        assert memory_service.activity_journal(tenant="other", limit=10) == []
        assert len(memory_service.activity_journal(tenant="acme", limit=10)) == 1

    def test_evaluations_never_leak_across_tenants(self):
        memory_service.record_evaluation(namespace="ns", useful=True, actor="h", tenant="acme")
        assert memory_service.recent_evaluations(tenant="other", limit=10) == []
        assert len(memory_service.recent_evaluations(tenant="acme", limit=10)) == 1


# ---------------------------------------------------------------------------
# Best-effort contract — record_* NEVER raise.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# growth_summary (Pollen data endpoints sprint) -- GET /v1/memory/growth.
# Real data derived from `memory_events` (op='store'); `authorship` is
# always None + documented (mem0 has no human-write path today -- see
# module docstring).
# ---------------------------------------------------------------------------


class TestGrowthSummary:
    def test_empty_is_zero_safe(self):
        summary = memory_service.growth_summary(tenant="default", days=30)
        assert summary["total"] == 0
        assert summary["memories_by_namespace"] == []
        assert summary["growth_series"] == []
        assert summary["by_actor"] == []
        assert summary["authorship"] is None
        assert isinstance(summary["source"], str) and summary["source"]

    def test_counts_only_store_events_not_search_or_read(self):
        memory_service.record_search(namespace="ns", query="q", result_count=1, actor="x")
        memory_service.record_read(namespace="ns", key="k", found=True, actor="x")
        memory_service.record_store(namespace="ns", key="k", actor="x")
        summary = memory_service.growth_summary(tenant="default", days=30)
        assert summary["total"] == 1

    def test_memories_by_namespace_grouped_and_sorted_desc(self):
        memory_service.record_store(namespace="ns-a", key="k1", actor="x")
        memory_service.record_store(namespace="ns-a", key="k2", actor="x")
        memory_service.record_store(namespace="ns-b", key="k3", actor="x")
        summary = memory_service.growth_summary(tenant="default", days=30)
        assert summary["memories_by_namespace"] == [
            {"namespace": "ns-a", "count": 2},
            {"namespace": "ns-b", "count": 1},
        ]

    def test_by_actor_reflects_invoking_role_not_human_agent_split(self):
        memory_service.record_store(namespace="ns", key="k1", actor="developer")
        memory_service.record_store(namespace="ns", key="k2", actor="developer")
        memory_service.record_store(namespace="ns", key="k3", actor="system")
        summary = memory_service.growth_summary(tenant="default", days=30)
        by_actor = {row["actor"]: row["count"] for row in summary["by_actor"]}
        assert by_actor == {"developer": 2, "system": 1}

    def test_authorship_always_none_never_fabricated(self):
        """mem0 has no human-initiated write path -- every recorded store
        event comes from the same automated plugin hook. A human/agent
        authorship split would be either fabricated (a fake 'human' count)
        or misleading (an always-zero 'human' count implying the split IS
        tracked) -- must always be None."""
        memory_service.record_store(namespace="ns", key="k1", actor="developer")
        summary = memory_service.growth_summary(tenant="default", days=30)
        assert summary["authorship"] is None

    def test_growth_series_buckets_by_day(self):
        from hivepilot.services import db

        memory_service.init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events (tenant, op, namespace, query_or_key, actor, ts) "
                    "VALUES (?, 'store', ?, ?, ?, ?)"
                ),
                ("default", "ns", "k1", "x", "2026-01-01 10:00:00"),
            )
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events (tenant, op, namespace, query_or_key, actor, ts) "
                    "VALUES (?, 'store', ?, ?, ?, ?)"
                ),
                ("default", "ns", "k2", "x", "2026-01-01 11:00:00"),
            )
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events (tenant, op, namespace, query_or_key, actor, ts) "
                    "VALUES (?, 'store', ?, ?, ?, ?)"
                ),
                ("default", "ns", "k3", "x", "2026-01-02 09:00:00"),
            )
        summary = memory_service.growth_summary(tenant="default", days=None)
        assert summary["growth_series"] == [
            {"date": "2026-01-01", "created": 2},
            {"date": "2026-01-02", "created": 1},
        ]

    def test_tenant_scoped(self):
        memory_service.record_store(namespace="ns", key="k1", actor="x", tenant="acme")
        memory_service.record_store(namespace="ns", key="k2", actor="x", tenant="other")
        assert memory_service.growth_summary(tenant="acme", days=30)["total"] == 1
        assert memory_service.growth_summary(tenant="other", days=30)["total"] == 1

    def test_admin_unscoped_tenant_none_sees_all(self):
        memory_service.record_store(namespace="ns", key="k1", actor="x", tenant="acme")
        memory_service.record_store(namespace="ns", key="k2", actor="x", tenant="other")
        assert memory_service.growth_summary(tenant=None, days=30)["total"] == 2


class TestRecordNeverRaises:
    def test_record_search_survives_db_failure(self, monkeypatch):
        from hivepilot.services import db

        def _boom(*args, **kwargs):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(db, "connect", _boom)
        # Must not raise.
        memory_service.record_search(namespace="ns", query="q", result_count=1, actor="x")

    def test_record_read_survives_db_failure(self, monkeypatch):
        from hivepilot.services import db

        def _boom(*args, **kwargs):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(db, "connect", _boom)
        memory_service.record_read(namespace="ns", key="k", found=True, actor="x")

    def test_record_store_survives_db_failure(self, monkeypatch):
        from hivepilot.services import db

        def _boom(*args, **kwargs):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(db, "connect", _boom)
        memory_service.record_store(namespace="ns", key="k", actor="x")

    def test_record_evaluation_survives_db_failure(self, monkeypatch):
        from hivepilot.services import db

        def _boom(*args, **kwargs):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(db, "connect", _boom)
        memory_service.record_evaluation(namespace="ns", useful=True, actor="x")

    def test_record_survives_weird_input(self):
        # Non-bool "useful" and None namespace must never raise — best-effort
        # contract holds even for malformed callers.
        memory_service.record_evaluation(namespace=None, useful="yes", actor=None)  # type: ignore[arg-type]
        memory_service.record_search(namespace=None, query=None, result_count=None, actor=None)  # type: ignore[arg-type]
