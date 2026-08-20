"""The ledger the ladder will eventually be built on, against a real database.

`test_pr_decision_resolution.py` patches these three functions away, so nothing
there would notice if the SQL itself were wrong. And the SQL is where this can
fail quietly: both writes carry `WHERE decision IS NULL`, the insert needs a
`UNIQUE (branch, tenant)` for its `ON CONFLICT` to be legal at all, and the one
caller wraps the whole thing in `except Exception: logger.warning(...)`.

That last part is what makes these tests necessary rather than thorough. A
broken statement here would not raise anywhere an operator looks -- it would
log a warning per pull request and leave the table empty, and the table being
empty is indistinguishable from "the gate and the human never disagreed". This
codebase's recurring defect is a zero that means "nothing wrote it down".
"""

from __future__ import annotations

import pytest

from hivepilot.services.state_service import (
    record_pr_gate_outcome,
    resolve_pr_gate_outcome,
    unresolved_pr_gate_outcomes,
)


def _row(branch="b", tenant="default"):
    from hivepilot.services import db

    with db.connect() as conn:
        raw = conn.execute(
            "SELECT gate_blocked, decision, pr_state, actor, run_id FROM pr_gate_outcomes "
            "WHERE branch = ? AND tenant = ?",
            (branch, tenant),
        ).fetchone()
        # `sqlite3.Row` never equals a tuple, and slicing it does -- which is
        # how a comparison here silently becomes an identity check.
        return None if raw is None else tuple(raw)


def _count(branch="b"):
    from hivepilot.services import db

    with db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM pr_gate_outcomes WHERE branch = ?", (branch,)
        ).fetchone()[0]


class TestTheStatementsExecuteAtAll:
    """The insert's `ON CONFLICT (branch, tenant)` is only legal if a matching
    UNIQUE constraint exists. Without one this raises -- into an `except` that
    logs a warning, leaving a permanently empty table that reads as consensus."""

    def test_recording_a_gate_verdict_writes_a_row(self):
        record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)

        assert _row()[:2] == (1, None)

    def test_a_recorded_row_is_unresolved(self):
        record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)

        pending = unresolved_pr_gate_outcomes(project="p")

        assert [(r["branch"], r["gate_blocked"]) for r in pending] == [("b", 1)]

    def test_resolving_it_writes_the_human_half(self):
        record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)
        resolve_pr_gate_outcome(
            branch="b", decision="override", pr_state="MERGED", actor="jeromesoyer"
        )

        assert _row() == (1, "override", "MERGED", "jeromesoyer", 7)
        assert unresolved_pr_gate_outcomes(project="p") == []


class TestOneBranchIsOneRow:
    def test_re_recording_does_not_double_count(self):
        """One run, one branch, one pull request. A second row would count the
        same decision twice in whatever eventually reads this."""
        for _ in range(3):
            record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)

        assert _count() == 1

    def test_re_recording_before_resolution_updates_the_verdict(self):
        """The human acts on the LAST thing the gate said, so that is what
        must be paired with their decision."""
        record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)
        record_pr_gate_outcome(run_id=9, project="p", branch="b", gate_blocked=False)

        assert _row()[0] == 0
        assert _row()[4] == 9


class TestADecisionIsNeverRewritten:
    """`WHERE decision IS NULL` on both writes. Not an optimisation: the pair
    (what the gate said, what the human did) is the measurement, and a later
    observation must not go back and edit either half."""

    def test_a_second_resolution_is_ignored(self):
        record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)
        resolve_pr_gate_outcome(branch="b", decision="override", pr_state="MERGED", actor="me")
        resolve_pr_gate_outcome(branch="b", decision="agreed", pr_state="CLOSED", actor="someone")

        assert _row()[1:4] == ("override", "MERGED", "me")

    def test_a_later_run_cannot_rewrite_the_gate_verdict_it_was_judged_on(self):
        """A branch reused by a later run must not retroactively change what
        the human was shown. This is the half that a plain UPSERT would lose."""
        record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)
        resolve_pr_gate_outcome(branch="b", decision="override", pr_state="MERGED", actor="me")

        record_pr_gate_outcome(run_id=99, project="p", branch="b", gate_blocked=False)

        assert _row()[0] == 1, "the gate verdict was rewritten under a recorded decision"
        assert _row()[4] == 7, "the run attribution moved to a run the human never saw"

    def test_a_resolved_row_stays_out_of_the_pending_list(self):
        record_pr_gate_outcome(run_id=7, project="p", branch="b", gate_blocked=True)
        resolve_pr_gate_outcome(branch="b", decision="agreed", pr_state="CLOSED", actor="me")
        record_pr_gate_outcome(run_id=99, project="p", branch="b", gate_blocked=True)

        assert unresolved_pr_gate_outcomes(project="p") == []

    def test_resolving_a_branch_that_was_never_recorded_is_a_no_op(self):
        """No row invented from an observation alone. Half a pair measures
        nothing, and a fabricated `gate_blocked` would measure the wrong thing."""
        resolve_pr_gate_outcome(
            branch="never-seen", decision="agreed", pr_state="MERGED", actor="me"
        )

        assert _count("never-seen") == 0


class TestItDoesNotLeakAcrossProjectsOrTenants:
    def test_the_project_filter_selects(self):
        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=True)
        record_pr_gate_outcome(run_id=2, project="other", branch="b2", gate_blocked=True)

        assert [r["branch"] for r in unresolved_pr_gate_outcomes(project="p")] == ["b"]

    def test_no_project_returns_every_project(self):
        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=True)
        record_pr_gate_outcome(run_id=2, project="other", branch="b2", gate_blocked=True)

        assert {r["branch"] for r in unresolved_pr_gate_outcomes()} == {"b", "b2"}

    def test_the_same_branch_name_in_two_tenants_is_two_rows(self):
        """`hivepilot/<project>/<run_id>` is unique per deployment, not
        globally -- two tenants can mint the identical name."""
        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=True)
        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=False, tenant="t2")

        assert _count() == 2
        assert _row(tenant="default")[0] == 1
        assert _row(tenant="t2")[0] == 0

    def test_resolving_one_tenant_leaves_the_other_open(self):
        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=True)
        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=True, tenant="t2")

        resolve_pr_gate_outcome(branch="b", decision="agreed", pr_state="CLOSED", actor="me")

        assert _row(tenant="t2")[1] is None
        assert [r["branch"] for r in unresolved_pr_gate_outcomes(tenant="t2")] == ["b"]


class TestGateBlockedIsStoredAsATruthValue:
    @pytest.mark.parametrize(
        ("given", "stored"), [(True, 1), (False, 0), (1, 1), (0, 0), ("yes", 1), ("", 0)]
    )
    def test_a_truthy_non_bool_does_not_arrive_as_a_string(self, given, stored):
        """`int(bool(...))` at the boundary. A raw `"yes"` in an INTEGER column
        would compare as 0 in SQL while reading as blocked in Python."""
        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=given)

        assert _row()[0] == stored


class TestTheTwoHalvesMeetOnARealDatabase:
    """Neither other test exercises the join. `test_pr_decision_resolution.py`
    patches the ledger away to test the sweep, and everything above calls the
    ledger directly without a sweep -- so both could pass while the two halves
    disagree about the shape of a row.

    Only the forge is faked here. `gate_blocked` comes back out of SQLite as an
    INTEGER, and the classifier's `merged != bool(gate_blocked)` has to survive
    that round trip.
    """

    def test_a_gate_verdict_recorded_now_is_resolved_by_a_later_sweep(self):
        from hivepilot.services.pr_decision import resolve_open_decisions

        class _Forge:
            def viewer_login(self, *, project):
                return "hivepilot-bot"

            def pr_outcome(self, *, project, branch):
                return {"blocked": ("MERGED", "jeromesoyer"), "promoted": ("CLOSED", None)}[branch]

        record_pr_gate_outcome(run_id=1, project="p", branch="blocked", gate_blocked=True)
        record_pr_gate_outcome(run_id=2, project="p", branch="promoted", gate_blocked=False)

        count = resolve_open_decisions(project_name="p", project=object(), forge=_Forge())

        assert count == 2
        # Both are disagreements, in OPPOSITE directions -- and the row still
        # says which, which is the whole reason `gate_blocked` is kept.
        assert _row("blocked")[:2] == (1, "override")
        assert _row("promoted")[:2] == (0, "override")
        assert unresolved_pr_gate_outcomes(project="p") == []

    def test_an_integer_gate_blocked_from_sqlite_still_classifies_as_agreement(self):
        """The polarity check that a fake-DB test cannot make: 1 and 0 arrive
        as ints, never bools, and `is not` would have inverted both."""
        from hivepilot.services.pr_decision import resolve_open_decisions

        class _Forge:
            def viewer_login(self, *, project):
                return "hivepilot-bot"

            def pr_outcome(self, *, project, branch):
                return ("MERGED", "jeromesoyer")

        record_pr_gate_outcome(run_id=1, project="p", branch="promoted", gate_blocked=False)

        resolve_open_decisions(project_name="p", project=object(), forge=_Forge())

        assert _row("promoted")[1] == "agreed"

    def test_a_deferred_sweep_leaves_the_row_recoverable(self):
        """The defer path against the real ledger: nothing written, and the
        row is still pending for the next run."""
        from hivepilot.services.pr_decision import resolve_open_decisions

        class _MuteForge:
            def viewer_login(self, *, project):
                return None

            def pr_outcome(self, *, project, branch):  # pragma: no cover
                raise AssertionError("observed a pull request it could not classify")

        record_pr_gate_outcome(run_id=1, project="p", branch="b", gate_blocked=True)

        assert resolve_open_decisions(project_name="p", project=object(), forge=_MuteForge()) == 0
        assert [r["branch"] for r in unresolved_pr_gate_outcomes(project="p")] == ["b"]
