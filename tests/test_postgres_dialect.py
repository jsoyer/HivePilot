"""The Postgres path, run against a real Postgres.

`hivepilot/services/db.py` has carried a complete-looking Postgres seam for a
long time -- `is_postgres`, `ph`, `autoincrement_pk`, `column_exists`,
`insert_returning_id` -- with sixteen unit tests. Every one of those asserts a
STRING: that `ph` turns `?` into `%s`, that `autoincrement_pk` returns
`BIGSERIAL PRIMARY KEY`. None of them ever opened a connection.

So the seam was plausible rather than proven, and `psycopg` was not declared in
any extra, meaning it could not be installed at all. That is this codebase's
signature defect: something that looks done because nothing has ever run it.

These tests do the only thing the unit tests cannot -- execute the schema and
the real read/write functions against a live server. They skip unless
`HIVEPILOT_DATABASE_URL` names one, so a normal `pytest` run is unaffected; CI
provides one in the `postgres` job.

A skip is not a pass. If the CI job stops providing the URL, these go quiet and
prove nothing, which is why the job asserts they did not skip.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HIVEPILOT_DATABASE_URL") or "").startswith(
        ("postgres://", "postgresql://")
    ),
    reason="needs HIVEPILOT_DATABASE_URL pointing at a live Postgres",
)


@pytest.fixture(autouse=True)
def _pg_settings(monkeypatch):
    """Point the process-global settings at the CI database.

    conftest's `_never_write_to_the_operators_home` and the sqlite fixtures do
    not apply here: every statement goes to the server named by the env var.
    """
    from hivepilot.config import settings

    monkeypatch.setattr(
        settings, "database_url", os.environ["HIVEPILOT_DATABASE_URL"], raising=False
    )


class TestTheSchemaActuallyBuilds:
    def test_init_db_creates_every_table(self):
        """The one thing sixteen string-comparison tests could not tell us.
        `autoincrement_pk()` returning the right FRAGMENT says nothing about
        whether the resulting `CREATE TABLE` parses."""
        from hivepilot.services import db
        from hivepilot.services.state_service import init_db

        init_db()

        with db.connect() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()

        names = {r["table_name"] for r in rows}
        # A representative spread rather than all 26: the point is that the
        # schema executed, not that this list stays in sync with it.
        assert {"runs", "steps", "interactions", "pr_gate_outcomes", "audit_log"} <= names

    def test_column_exists_answers_from_information_schema(self):
        """The SQLite branch uses `PRAGMA table_info`, which does not exist
        here. Only a live server distinguishes the two branches."""
        from hivepilot.services import db
        from hivepilot.services.state_service import init_db

        init_db()

        with db.connect() as conn:
            assert db.column_exists(conn, "pr_gate_outcomes", "gate_blocked")
            assert not db.column_exists(conn, "pr_gate_outcomes", "no_such_column")


class TestTheLedgerRoundTrips:
    """`pr_gate_outcomes` is the newest table and the one the autonomy ladder
    reads. Its writes use `ON CONFLICT ... DO UPDATE ... WHERE`, which SQLite
    and Postgres both support but parse differently."""

    def test_record_list_resolve(self):
        from hivepilot.services.state_service import (
            init_db,
            record_pr_gate_outcome,
            resolve_pr_gate_outcome,
            unresolved_pr_gate_outcomes,
        )

        init_db()
        branch = "hivepilot/pgtest/1"
        record_pr_gate_outcome(run_id=1, project="pg", branch=branch, gate_blocked=True)

        pending = [r for r in unresolved_pr_gate_outcomes(project="pg") if r["branch"] == branch]
        assert pending, "the row did not come back as unresolved"

        resolve_pr_gate_outcome(
            branch=branch, decision="override", pr_state="MERGED", actor="jeromesoyer"
        )

        assert not [r for r in unresolved_pr_gate_outcomes(project="pg") if r["branch"] == branch]

    def test_a_decision_is_not_rewritten_here_either(self):
        """`WHERE decision IS NULL` on both writes. Same invariant as SQLite,
        different planner -- worth proving rather than assuming."""
        from hivepilot.services import db
        from hivepilot.services.state_service import (
            init_db,
            record_pr_gate_outcome,
            resolve_pr_gate_outcome,
        )

        init_db()
        branch = "hivepilot/pgtest/2"
        record_pr_gate_outcome(run_id=1, project="pg", branch=branch, gate_blocked=True)
        resolve_pr_gate_outcome(branch=branch, decision="override", pr_state="MERGED", actor="me")
        resolve_pr_gate_outcome(branch=branch, decision="agreed", pr_state="CLOSED", actor="other")
        record_pr_gate_outcome(run_id=99, project="pg", branch=branch, gate_blocked=False)

        with db.connect() as conn:
            row = conn.execute(
                db.ph(
                    "SELECT gate_blocked, decision, actor FROM pr_gate_outcomes WHERE branch = ?"
                ),
                (branch,),
            ).fetchone()

        assert row["decision"] == "override"
        assert row["actor"] == "me"
        assert bool(row["gate_blocked"]) is True


class TestInsertReturningIdWorksOnTheServer:
    def test_a_run_gets_a_real_id(self):
        """`insert_returning_id` appends `RETURNING id` on Postgres instead of
        reading `cursor.lastrowid`. The two branches share no code, so the
        SQLite tests say nothing about this one."""
        from hivepilot.services.state_service import create_run, init_db

        init_db()

        run_id = create_run(project="pg", task="portability probe")

        assert isinstance(run_id, int) and run_id > 0
