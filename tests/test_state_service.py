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


class TestRecordStepAttribution:
    """A step no agent performed must not carry an agent.

    Role is declared per *task*, but a task's stages can mix agent work with
    plain shell commands. Persisting the task's role on a `shell` step would
    credit an agent with work no model did — inflating its step count with
    activity it never performed, and making the Agents panel's per-agent
    figures mean less the more shell a pipeline uses.

    Enforced here, at the single point where role is written, rather than in
    each caller: an invariant every call site has to remember is one that
    eventually gets forgotten.
    """

    def test_a_shell_step_never_carries_a_role(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(run_id, "signals", "success", provider="shell", role="groomer")
        assert get_steps_for_run(run_id)[0]["role"] is None

    def test_a_failed_shell_step_never_carries_a_role(self) -> None:
        """Failing does not turn a shell command into agent work."""
        run_id = record_run_start("proj", "task")
        record_step(run_id, "signals", "failed", "boom", provider="shell", role="groomer")
        assert get_steps_for_run(run_id)[0]["role"] is None

    def test_a_model_step_keeps_its_role(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(run_id, "propose", "success", provider="claude", role="groomer")
        assert get_steps_for_run(run_id)[0]["role"] == "groomer"

    def test_an_unknown_provider_keeps_its_role(self) -> None:
        """Only providers *known* not to invoke a model strip attribution.

        A NULL provider is a telemetry gap, not proof that no agent ran.
        Dropping the role there would silently erase real attribution to
        protect against a case we cannot even confirm — so the uncertain
        direction is to keep it.
        """
        run_id = record_run_start("proj", "task")
        record_step(run_id, "mystery", "success", provider=None, role="groomer")
        assert get_steps_for_run(run_id)[0]["role"] == "groomer"


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


class TestStepsCacheTokenMigration:
    """usage-capture-modelusage fix: cache-read/cache-creation tokens are
    billed at different rates than base input/output tokens and are now
    captured -- must be persisted as their OWN columns, same additive
    ALTER TABLE ... ADD COLUMN idiom as input_tokens/output_tokens/cost_usd."""

    def test_columns_exist_after_init_db(self) -> None:
        init_db()
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "cache_read_tokens")
            assert db.column_exists(conn, "steps", "cache_creation_tokens")

    def test_pre_existing_db_without_cache_columns_gets_them(self) -> None:
        state_service.init_db()
        with db.connect() as conn:
            conn.execute("ALTER TABLE steps DROP COLUMN cache_read_tokens")
            conn.execute("ALTER TABLE steps DROP COLUMN cache_creation_tokens")
            conn.execute(
                "INSERT INTO steps (run_id, step, status, provider, model) VALUES (?, ?, ?, ?, ?)",
                (1, "legacy-step", "success", "claude", "claude-sonnet-4-6"),
            )
        with db.connect() as conn:
            assert not db.column_exists(conn, "steps", "cache_read_tokens")

        init_db()  # idempotent migration must backfill the missing columns

        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "cache_read_tokens")
            assert db.column_exists(conn, "steps", "cache_creation_tokens")
            row = conn.execute("SELECT * FROM steps WHERE step='legacy-step'").fetchone()
        assert row is not None
        assert row["cache_read_tokens"] is None
        assert row["cache_creation_tokens"] is None


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

    def test_persists_cache_tokens_when_given(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(
            run_id,
            "s1",
            "success",
            provider="claude",
            model="claude-sonnet-4-5",
            input_tokens=10,
            output_tokens=50,
            cache_read_tokens=40000,
            cache_creation_tokens=2000,
            cost_usd=0.05,
        )
        rows = get_steps_for_run(run_id)
        assert rows[0]["cache_read_tokens"] == 40000
        assert rows[0]["cache_creation_tokens"] == 2000

    def test_cache_tokens_null_when_omitted(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "success", provider="claude", model="claude-sonnet-4-6")
        rows = get_steps_for_run(run_id)
        assert rows[0]["cache_read_tokens"] is None
        assert rows[0]["cache_creation_tokens"] is None

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
# Mirador Agent Panels backend sprint — steps.role (idempotent migration +
# record_step persistence). Mirrors the provider/model and usage migrations
# above exactly: additive ALTER TABLE ... ADD COLUMN, guarded by
# column_exists, NULL for every pre-migration row -- never backfilled with a
# guess.
# ---------------------------------------------------------------------------


class TestStepsRoleMigration:
    def test_column_exists_after_init_db(self) -> None:
        init_db()
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "role")

    def test_init_db_is_idempotent(self) -> None:
        init_db()
        init_db()  # must not raise "duplicate column name"
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "role")

    def test_pre_existing_db_without_role_column_gets_it(self) -> None:
        """Simulates a pre-this-sprint DB (has provider/model/usage columns
        but not role): recreate steps in that shape, confirm init_db()
        backfills the column without touching existing rows."""
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
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost_usd REAL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO steps (run_id, step, status, provider, model) VALUES (?, ?, ?, ?, ?)",
                (1, "legacy-step", "success", "claude", "claude-sonnet-4-6"),
            )

        with db.connect() as conn:
            assert not db.column_exists(conn, "steps", "role")

        init_db()  # idempotent migration must backfill the missing column

        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "role")
            row = conn.execute("SELECT * FROM steps WHERE step='legacy-step'").fetchone()
        assert row is not None
        assert row["provider"] == "claude"  # existing row untouched
        assert row["role"] is None  # backfilled -> NULL, never an invented role


class TestRecordStepRole:
    def test_persists_role_when_given(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "success", role="developer")
        rows = get_steps_for_run(run_id)
        assert rows[0]["role"] == "developer"

    def test_role_null_when_omitted(self) -> None:
        """Backward-compat: existing callers that never pass role still
        work and persist NULL — never an invented value."""
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "success")
        rows = get_steps_for_run(run_id)
        assert rows[0]["role"] is None

    def test_fully_backward_compat_positional_call(self) -> None:
        run_id = record_run_start("proj", "task")
        record_step(run_id, "s1", "failed", "boom")
        rows = get_steps_for_run(run_id)
        assert rows[0]["detail"] == "boom"
        assert rows[0]["role"] is None


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint — state_service.list_verdicts /
# list_lessons_by_tenant: tenant-scoped, role-filterable reads backing the
# new GET /v1/verdicts / GET /v1/lessons endpoints. Neither `verdicts` nor
# `lessons` carries its own `tenant` column -- both resolve it via a LEFT
# JOIN back to `runs.tenant` on `run_id`. A concrete `tenant` filter is
# fail-closed: a row whose run_id is NULL or doesn't match ANY run can never
# satisfy `r.tenant=?`, so it is excluded from every non-admin (tenant-
# scoped) call -- only an unscoped (`tenant=None`, admin) call can see a
# row whose tenant is unresolvable.
# ---------------------------------------------------------------------------


class TestListVerdicts:
    def test_tenant_isolation_via_run_join(self) -> None:
        run_acme = record_run_start("p", "t", tenant="acme")
        run_other = record_run_start("p", "t", tenant="other")
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
        rows = state_service.list_verdicts(tenant="acme")
        assert len(rows) == 1
        assert rows[0]["decision"] == "approve"

    def test_unscoped_admin_sees_all_tenants(self) -> None:
        run_acme = record_run_start("p", "t", tenant="acme")
        run_other = record_run_start("p", "t", tenant="other")
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
        rows = state_service.list_verdicts(tenant=None)
        assert len(rows) == 2

    def test_role_filter(self) -> None:
        run_id = record_run_start("p", "t", tenant="acme")
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
        rows = state_service.list_verdicts(tenant="acme", role="reviewer")
        assert len(rows) == 1
        assert rows[0]["role"] == "reviewer"

    def test_orphan_run_id_excluded_from_tenant_scoped_query(self) -> None:
        """A verdict pointing at a run_id that doesn't exist (or a NULL
        run_id) can never resolve a tenant -- must be excluded from a
        concrete tenant filter (fail closed), never misattributed."""
        state_service.record_verdict(
            run_id=None,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="approve",
            confidence=0.9,
        )
        assert state_service.list_verdicts(tenant="acme") == []
        assert len(state_service.list_verdicts(tenant=None)) == 1

    def test_does_not_weaken_list_recent_verdicts(self) -> None:
        """list_recent_verdicts stays exactly as before -- unfiltered by
        tenant, run_id-filterable only."""
        run_id = record_run_start("p", "t", tenant="acme")
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="reviewer",
            kind="review",
            decision="approve",
            confidence=0.9,
        )
        rows = state_service.list_recent_verdicts(run_id=run_id)
        assert len(rows) == 1


class TestListLessonsByTenant:
    def test_tenant_isolation_via_run_join(self) -> None:
        run_acme = record_run_start("p", "t", tenant="acme")
        run_other = record_run_start("p", "t", tenant="other")
        state_service.record_lesson(
            run_id=run_acme,
            project="p",
            role="developer",
            task="t",
            text="lesson acme",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        state_service.record_lesson(
            run_id=run_other,
            project="p",
            role="developer",
            task="t",
            text="lesson other",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        rows = state_service.list_lessons_by_tenant(tenant="acme")
        assert len(rows) == 1
        assert rows[0]["text"] == "lesson acme"

    def test_unscoped_admin_sees_all_tenants(self) -> None:
        run_acme = record_run_start("p", "t", tenant="acme")
        run_other = record_run_start("p", "t", tenant="other")
        state_service.record_lesson(
            run_id=run_acme,
            project="p",
            role="developer",
            task="t",
            text="lesson acme",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        state_service.record_lesson(
            run_id=run_other,
            project="p",
            role="developer",
            task="t",
            text="lesson other",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        assert len(state_service.list_lessons_by_tenant(tenant=None)) == 2

    def test_role_filter(self) -> None:
        run_id = record_run_start("p", "t", tenant="acme")
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
        rows = state_service.list_lessons_by_tenant(tenant="acme", role="reviewer")
        assert len(rows) == 1
        assert rows[0]["role"] == "reviewer"

    def test_orphan_run_id_excluded_from_tenant_scoped_query(self) -> None:
        state_service.record_lesson(
            run_id=None,
            project="p",
            role="developer",
            task="t",
            text="orphan lesson",
            score=0.5,
            confidence=0.5,
            category="test",
        )
        assert state_service.list_lessons_by_tenant(tenant="acme") == []
        assert len(state_service.list_lessons_by_tenant(tenant=None)) == 1

    def test_does_not_weaken_list_lessons(self) -> None:
        """list_lessons stays exactly as before -- project-required,
        validated_only defaulting to True."""
        run_id = record_run_start("p", "t", tenant="acme")
        state_service.record_lesson(
            run_id=run_id,
            project="lessons-proj",
            role="developer",
            task="t",
            text="candidate",
            score=None,
            confidence=None,
            category="test",
            validated=False,
        )
        assert state_service.list_lessons("lessons-proj") == []  # validated_only=True default
        assert len(state_service.list_lessons("lessons-proj", validated_only=False)) == 1


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


# ---------------------------------------------------------------------------
# init_db() concurrency race — the S3 async-runs feature calls init_db() from
# both a background worker thread and the request thread against the same
# sqlite file. The check-then-ALTER pattern (`if not column_exists: ALTER
# TABLE ... ADD COLUMN`) is a TOCTOU race: two threads can both observe the
# column missing and both attempt the ALTER, and the loser raises
# `sqlite3.OperationalError: duplicate column name`. init_db() must be safe
# to call concurrently from multiple threads against a fresh (or existing)
# DB file.
# ---------------------------------------------------------------------------


class TestInitDbConcurrency:
    def test_concurrent_init_db_calls_do_not_raise_duplicate_column(self) -> None:
        import threading

        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def _worker() -> None:
            try:
                barrier.wait(timeout=5)
                init_db()
            except BaseException as exc:  # noqa: BLE001 - capture for assertion
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"init_db() raised under concurrency: {errors!r}"

        # Schema must still be fully migrated after the concurrent race.
        with db.connect() as conn:
            assert db.column_exists(conn, "steps", "provider")
            assert db.column_exists(conn, "steps", "model")
            assert db.column_exists(conn, "steps", "input_tokens")
            assert db.column_exists(conn, "steps", "output_tokens")
            assert db.column_exists(conn, "steps", "cost_usd")
            assert db.column_exists(conn, "retry_queue", "context")
            assert db.column_exists(conn, "runs", "tenant")
            assert db.column_exists(conn, "approvals", "tenant")
            assert db.column_exists(conn, "audit_log", "tenant")
            assert db.column_exists(conn, "tokens", "tenant")


class TestMarkSwarmEventFailed:
    """Bug-debt fix: a claimed swarm event whose handler raises must land in
    a DEFINED terminal state (`failed`), never stay `running` forever with
    no reaper -- and, like `done`/`skipped`, must never be re-claimable
    (`claim_swarm_event` only ever matches `status='pending'`)."""

    def _running_event(self, event_id: str = "pr_ready:x") -> None:
        from hivepilot.swarm.models import Event, compute_event_id

        event = Event(
            id=compute_event_id("pr_ready", event_id),
            type="pr_ready",
            payload={"repo": "acme/widgets"},
            tenant="default",
            origin_instance="inst-a",
        )
        state_service.insert_swarm_event(event)
        assert state_service.claim_swarm_event(event.id, claimed_by="inst-a") is True
        assert state_service.mark_swarm_event_running(event.id, claimed_by="inst-a") is True

    def test_running_event_transitions_to_failed(self) -> None:
        from hivepilot.swarm.models import compute_event_id

        self._running_event("r:fail:1")
        event_id = compute_event_id("pr_ready", "r:fail:1")

        assert state_service.mark_swarm_event_failed(event_id) is True
        row = state_service.get_swarm_event(event_id)
        assert row is not None
        assert row["status"] == "failed"

    def test_non_running_event_is_a_no_op(self) -> None:
        from hivepilot.swarm.models import Event, compute_event_id

        event = Event(
            id=compute_event_id("pr_ready", "r:fail:2"),
            type="pr_ready",
            payload={},
            tenant="default",
            origin_instance="inst-a",
        )
        state_service.insert_swarm_event(event)  # still `pending`, never claimed
        assert state_service.mark_swarm_event_failed(event.id) is False
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "pending"

    def test_failed_event_can_never_be_reclaimed(self) -> None:
        from hivepilot.swarm.models import compute_event_id

        self._running_event("r:fail:3")
        event_id = compute_event_id("pr_ready", "r:fail:3")
        assert state_service.mark_swarm_event_failed(event_id) is True
        assert state_service.claim_swarm_event(event_id, claimed_by="inst-b") is False


# ---------------------------------------------------------------------------
# list_all_runs / list_recent_runs — deterministic "most recent run" ordering
# (Pollen graph-cascade rebuild). `started_at` is SQLite `CURRENT_TIMESTAMP`
# (SECOND resolution) — two runs recorded within the same wall-clock second
# (routine in tests, and possible in fast-firing real usage) previously had
# NO deterministic tiebreak, so "the last run" was whichever row SQLite
# happened to return first for a tied `ORDER BY started_at DESC` — a real
# correctness gap for `pipeline_source.py`'s "last run" resolution and any
# other caller relying on recency. `id` (the table's autoincrement PK) is
# monotonically increasing and always distinct, so `ORDER BY started_at
# DESC, id DESC` makes ties resolve to the row inserted LAST, deterministically.
# ---------------------------------------------------------------------------


class TestRunRecencyOrdering:
    def _force_same_started_at(self, run_ids: list[int]) -> None:
        """Directly rewrite `started_at` to an IDENTICAL value for every id
        in *run_ids* — simulates two runs recorded within the same SQLite
        `CURRENT_TIMESTAMP` second, which real usage can hit but a fast unit
        test normally can't reproduce via timing alone."""
        with db.connect() as conn:
            for run_id in run_ids:
                conn.execute(
                    db.ph("UPDATE runs SET started_at=? WHERE id=?"),
                    ("2026-01-01 00:00:00", run_id),
                )

    def test_list_all_runs_breaks_started_at_tie_by_id_desc(self) -> None:
        run_a = record_run_start("demo", "demo")
        run_b = record_run_start("demo", "demo")
        self._force_same_started_at([run_a, run_b])

        runs = state_service.list_all_runs()
        tied = [r for r in runs if r["id"] in (run_a, run_b)]
        assert [r["id"] for r in tied] == [run_b, run_a], (
            "tied started_at must resolve to the LAST-inserted run first (id DESC)"
        )

    def test_list_recent_runs_breaks_started_at_tie_by_id_desc(self) -> None:
        run_a = record_run_start("demo", "demo")
        run_b = record_run_start("demo", "demo")
        self._force_same_started_at([run_a, run_b])

        runs = state_service.list_recent_runs()
        tied = [r for r in runs if r["id"] in (run_a, run_b)]
        assert [r["id"] for r in tied] == [run_b, run_a]
