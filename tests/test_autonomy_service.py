"""The autonomy ladder must not read an empty table as a verdict on the agents.

Measured on the production database while building this:

    approvals   run ids 243 .. 321   (10 rows, all "approved")
    verdicts    run ids 438 .. 467   (26 rows, 7 carrying a decision)

`agreement_rows` returns zero, and the join key is not why. The two populations
do not overlap at all: verdicts come from the adversarial review, approvals
from the destructive-step gate, and no single run has ever produced both.
Every verdict also carries a NULL `pipeline_run_id` -- they predate the column.

So the interesting behaviour is not the arithmetic. It is that a 0% built from
nothing must not render like a 0% built from two hundred observations, because
the decision they invite -- how much autonomy a role gets -- is opposite.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    from hivepilot.services import state_service

    state_service.init_db()
    return tmp_path / "s.db"


def _verdict(*, run_id, role, decision, pipeline_run_id=None):
    from hivepilot.services import state_service

    return state_service.record_verdict(
        run_id=run_id,
        pipeline_run_id=pipeline_run_id,
        project="p",
        task="t",
        role=role,
        kind="review",
        decision=decision,
        confidence=0.9,
    )


def _approval(*, run_id, status):
    from hivepilot.services import db, state_service

    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("INSERT INTO approvals (run_id, project, task, status) VALUES (?,?,?,?)"),
            (run_id, "p", "t", status),
        )
        conn.commit()


class TestAnEmptyReportExplainsItself:
    def test_nothing_recorded_at_all_says_so(self, db_path):
        from hivepilot.services import autonomy_service

        report = autonomy_service.agreement_report()

        assert report.roles == []
        assert report.empty_reason is not None
        assert "no verdict" in report.empty_reason

    def test_verdicts_without_a_pipeline_run_id_are_named_as_the_cause(self, db_path):
        """The production shape, exactly: decisions recorded, key absent."""
        from hivepilot.services import autonomy_service

        _verdict(run_id=467, role="review", decision="BLOCKED")
        _approval(run_id=321, status="approved")

        report = autonomy_service.agreement_report()

        assert report.empty_reason is not None
        assert "pipeline run id" in report.empty_reason

    def test_disjoint_populations_are_named_as_the_cause(self, db_path):
        """Both sides populated, keys present, and still no run has both."""
        from hivepilot.services import autonomy_service

        _verdict(run_id=467, role="review", decision="BLOCKED", pipeline_run_id=467)
        _approval(run_id=321, status="approved")

        report = autonomy_service.agreement_report()

        assert report.empty_reason is not None
        assert "co-occurred" in report.empty_reason

    def test_the_rendering_refuses_to_show_a_rung(self, db_path):
        from hivepilot.services import autonomy_service

        text = "\n".join(autonomy_service.render_report(autonomy_service.agreement_report()))

        assert "NOT MEASURABLE" in text
        assert "0%" not in text


class TestAgreementIsCountedFailClosed:
    def test_a_pending_approval_is_never_agreement(self, db_path):
        """An unanswered gate is not consent."""
        from hivepilot.services import autonomy_service

        assert autonomy_service._classify("ACCEPT", "pending") == "unclassifiable"

    def test_an_unknown_verdict_is_never_agreement(self, db_path):
        from hivepilot.services import autonomy_service

        assert autonomy_service._classify("MAYBE", "approved") == "unclassifiable"

    def test_an_empty_verdict_is_never_agreement(self, db_path):
        """Absent output must not read as endorsement -- the gate treats an
        unparseable verdict as proceed, and this must NOT inherit that."""
        from hivepilot.services import autonomy_service

        assert autonomy_service._classify(None, "approved") == "unclassifiable"
        assert autonomy_service._classify("", "approved") == "unclassifiable"

    def test_both_stopping_is_agreement(self, db_path):
        from hivepilot.services import autonomy_service

        assert autonomy_service._classify("BLOCKED", "rejected") == "agreed"

    def test_both_proceeding_is_agreement(self, db_path):
        from hivepilot.services import autonomy_service

        assert autonomy_service._classify("ACCEPT", "approved") == "agreed"

    def test_opposite_calls_are_disagreement(self, db_path):
        from hivepilot.services import autonomy_service

        assert autonomy_service._classify("BLOCKED", "approved") == "disagreed"


class TestTheSampleSizeTravelsWithTheRate:
    def test_a_small_perfect_score_is_not_sufficient_evidence(self, db_path):
        """Eight out of eight is not evidence. A ladder that promotes on it
        promotes on noise."""
        from hivepilot.services import autonomy_service

        for run in range(600, 608):
            _verdict(run_id=run, role="reviewer", decision="ACCEPT", pipeline_run_id=run)
            _approval(run_id=run, status="approved")

        report = autonomy_service.agreement_report()
        reviewer = next(r for r in report.roles if r.role == "reviewer")

        assert reviewer.rate == 1.0
        assert reviewer.comparable == 8
        assert reviewer.has_enough_evidence is False
        assert report.measurable is False

    def test_enough_observations_becomes_measurable(self, db_path):
        from hivepilot.services import autonomy_service

        for run in range(700, 700 + autonomy_service.MIN_SAMPLE):
            _verdict(run_id=run, role="reviewer", decision="ACCEPT", pipeline_run_id=run)
            _approval(run_id=run, status="approved")

        report = autonomy_service.agreement_report()

        assert report.measurable is True
        assert "INSUFFICIENT" not in "\n".join(autonomy_service.render_report(report))

    def test_unclassifiable_rows_are_reported_not_dropped(self, db_path):
        """Dropping them would quietly shrink the denominator and flatter the
        role."""
        from hivepilot.services import autonomy_service

        _verdict(run_id=800, role="reviewer", decision="ACCEPT", pipeline_run_id=800)
        _approval(run_id=800, status="approved")
        _verdict(run_id=801, role="reviewer", decision="WHATEVER", pipeline_run_id=801)
        _approval(run_id=801, status="approved")

        reviewer = next(r for r in autonomy_service.agreement_report().roles)

        assert reviewer.comparable == 1
        assert reviewer.unclassifiable == 1
        assert "does not recognise" in "\n".join(
            autonomy_service.render_report(autonomy_service.agreement_report())
        )


class TestItGrantsNothing:
    def test_the_report_says_so_out_loud(self, db_path):
        """The ladder reports; the operator promotes. An automated authority
        increase driven by a self-reported score has no outside check on it."""
        from hivepilot.services import autonomy_service

        for run in range(900, 900 + autonomy_service.MIN_SAMPLE):
            _verdict(run_id=run, role="reviewer", decision="ACCEPT", pipeline_run_id=run)
            _approval(run_id=run, status="approved")

        text = "\n".join(autonomy_service.render_report(autonomy_service.agreement_report()))

        assert "operator action" in text
