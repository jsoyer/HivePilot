"""Tests for the blocked-gate report posted onto the pull request.

Measured on PR #428 before this existed: `reviews: 0, comments: 0`, and a
43-character body reading "Automated pull request opened by HivePilot." The
reviewer, CISO and QA had all run and produced 7 735, 13 958 and 9 702
characters of findings. None of it was anywhere a human would look.

The stored verdict was the whole problem in one line:

    "adversarial review by reviewer, ciso, qa:
     reviewer: REQUEST_CHANGES; ciso: BLOCKED; qa: REQUEST_CHANGES"

Three status tokens and not one reason. A gate that says BLOCKED without
saying why moves the work of finding out onto the person least able to do it.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def run_with_reviews(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    from hivepilot.services import state_service

    run_id = state_service.record_run_start(project="noxys", task="noxys")
    for actor, role, summary in (
        (
            "Victor (Reviewer)",
            "reviewer",
            "status: REQUEST_CHANGES\nThe grant path never checks isAdmin, so any signed-in user can revoke.",
        ),
        (
            "Hugo (CISO)",
            "ciso",
            "status: BLOCKED\nFour HIGH findings: the allowlist accepts an unsigned binary path.",
        ),
        ("Marie (QA)", "qa", "status: REQUEST_CHANGES\nNo test covers the revoke path at all."),
        ("Théo (Documentation)", "documentation", "status: PASS\nDocs updated."),
    ):
        state_service.record_interaction(
            actor=actor,
            action="completed stage",
            target=None,
            summary=summary,
            run_id=run_id,
            metadata={"pipeline": "noxys", "role": role},
        )
    return run_id


class TestReportCarriesTheReasons:
    def test_names_why_each_blocking_role_blocked(self, run_with_reviews):
        """The whole point. A status token is not a reason."""
        from hivepilot.services import pr_verdict_report

        report = pr_verdict_report.build_report(
            run_id=run_with_reviews,
            verdict_summary="reviewer: REQUEST_CHANGES; ciso: BLOCKED; qa: REQUEST_CHANGES",
        )

        assert "never checks isAdmin" in report
        assert "unsigned binary path" in report
        assert "No test covers the revoke path" in report

    def test_lists_every_role_and_its_status(self, run_with_reviews):
        from hivepilot.services import pr_verdict_report

        report = pr_verdict_report.build_report(
            run_id=run_with_reviews,
            verdict_summary="reviewer: REQUEST_CHANGES; ciso: BLOCKED; qa: REQUEST_CHANGES",
        )

        for role in ("reviewer", "ciso", "qa"):
            assert role in report.lower()

    def test_a_passing_role_does_not_get_its_whole_output_pasted(self, run_with_reviews):
        """Only the blockers need explaining; the rest is noise on a PR."""
        from hivepilot.services import pr_verdict_report

        report = pr_verdict_report.build_report(
            run_id=run_with_reviews,
            verdict_summary="reviewer: REQUEST_CHANGES; ciso: BLOCKED; qa: REQUEST_CHANGES",
        )

        assert "Docs updated." not in report

    def test_bounded_and_says_so(self, tmp_path, monkeypatch):
        """A CISO answer ran to 13 958 characters; GitHub is not a log sink."""
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        from hivepilot.services import pr_verdict_report, state_service

        run_id = state_service.record_run_start(project="p", task="t")
        state_service.record_interaction(
            actor="Hugo (CISO)",
            action="completed stage",
            target=None,
            summary="status: BLOCKED\n" + ("x" * 50_000),
            run_id=run_id,
            metadata={"role": "ciso"},
        )

        report = pr_verdict_report.build_report(run_id=run_id, verdict_summary="ciso: BLOCKED")

        assert len(report) < 20_000
        assert "truncated" in report.lower()

    def test_redacts_a_registered_secret(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        from hivepilot.services import pr_verdict_report, state_service
        from hivepilot.services.config_provenance import register_secret_value

        register_secret_value("sk-pr-report-secret-7788990011")
        run_id = state_service.record_run_start(project="p", task="t")
        state_service.record_interaction(
            actor="Hugo (CISO)",
            action="completed stage",
            target=None,
            summary="status: BLOCKED\nthe token sk-pr-report-secret-7788990011 is hardcoded",
            run_id=run_id,
            metadata={"role": "ciso"},
        )

        report = pr_verdict_report.build_report(run_id=run_id, verdict_summary="ciso: BLOCKED")

        assert "sk-pr-report-secret-7788990011" not in report

    def test_no_blocking_role_means_no_report(self, run_with_reviews):
        """Nothing to explain is nothing to post. A gate that comments on every
        green run trains people to ignore its comments."""
        from hivepilot.services import pr_verdict_report

        assert (
            pr_verdict_report.build_report(
                run_id=run_with_reviews, verdict_summary="reviewer: APPROVE; ciso: PASS"
            )
            is None
        )

    def test_missing_interaction_still_reports_the_status(self, tmp_path, monkeypatch):
        """A role whose output was not recorded must still appear.

        Dropping it would understate the blockers, and a partial list is worse
        than a bare status because it looks complete.
        """
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        from hivepilot.services import pr_verdict_report, state_service

        run_id = state_service.record_run_start(project="p", task="t")

        report = pr_verdict_report.build_report(run_id=run_id, verdict_summary="ciso: BLOCKED")

        assert report is not None
        assert "ciso" in report.lower()
        assert "BLOCKED" in report


class TestGatePostsTheReport:
    """The gate had the material and the plumbing and used neither.

    `forge.comment_pr` already existed. `git_service`'s own comment said the
    review report belongs on the PR. Nothing connected the two, so PR #428 sat
    blocked and silent for three days.
    """

    def _git(self, **kw):
        from hivepilot.models import GitActions

        return GitActions(create_pr=True, promote_pr=True, **kw)

    def test_a_blocked_gate_comments_on_the_pr(self, run_with_reviews, monkeypatch):
        from hivepilot.services import git_service

        posted: list[str] = []

        class Forge:
            def open_pr(self, **kw):
                return "https://example.invalid/pr/1"

            def promote_pr(self, **kw):
                pass

            def comment_pr(self, *, project, branch, body):
                posted.append(body)

        monkeypatch.setattr(git_service, "_forge_for", lambda project: Forge(), raising=False)

        body = git_service.post_blocked_report(
            forge=Forge(),
            project=object(),
            branch="hivepilot/noxys",
            run_id=run_with_reviews,
            verdict_summary="reviewer: REQUEST_CHANGES; ciso: BLOCKED",
            post=posted.append,
        )

        assert body is not None
        assert "never checks isAdmin" in posted[0]

    def test_a_clean_gate_posts_nothing(self, run_with_reviews):
        from hivepilot.services import git_service

        posted: list[str] = []
        body = git_service.post_blocked_report(
            forge=None,
            project=object(),
            branch="b",
            run_id=run_with_reviews,
            verdict_summary="reviewer: APPROVE",
            post=posted.append,
        )

        assert body is None
        assert posted == []

    def test_a_forge_failure_never_breaks_the_run(self, run_with_reviews):
        """Posting is reporting. A comment that cannot be delivered must not
        take down the git actions that matter."""
        from hivepilot.services import git_service

        def boom(_body):
            raise RuntimeError("github is down")

        git_service.post_blocked_report(
            forge=None,
            project=object(),
            branch="b",
            run_id=run_with_reviews,
            verdict_summary="ciso: BLOCKED",
            post=boom,
        )


class TestAStageBlockNamesTheRealRole:
    """Posted on a real PR and carried no reasoning at all:

        | `stage` | 🚫 BLOCK |
        _This role blocked, but its output was not recorded for this run_

    When the block comes from the STAGE's own `status:` line rather than a
    judge verdict, the summary fell back to the literal string "stage". No
    interaction is recorded under that name, so the report quoted nothing —
    which is exactly the "you cannot just say BLOCKED" this feature exists to
    prevent.

    The stage's role is known at the call site; using it makes the output
    findable.
    """

    def test_the_stage_role_is_used_so_its_output_is_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
        from hivepilot.services import pr_verdict_report, state_service

        run_id = state_service.record_run_start("p", "t")
        state_service.record_interaction(
            actor="Victor (Reviewer)",
            action="completed stage",
            target=None,
            summary="status: REQUEST_CHANGES\nThe grant path never checks isAdmin.",
            run_id=run_id,
            metadata={"role": "reviewer"},
        )

        report = pr_verdict_report.build_report(
            run_id=run_id, verdict_summary="reviewer: REQUEST_CHANGES"
        )

        assert "never checks isAdmin" in report
        assert "was not recorded" not in report

    def test_a_summary_naming_stage_is_a_smell_we_no_longer_emit(self):
        """`stage` is not a role and never will be findable."""
        from hivepilot.services import git_service

        summary = git_service._verdict_role_summary(None, "status: BLOCK", role="reviewer")

        assert summary is not None
        assert summary.startswith("reviewer:")
        assert "stage:" not in summary
