"""A pipeline whose gate IS its deterministic checks.

Config PR #73 (14 August) removed forage's release manager on an argument worth
keeping: *"Rewriting that prompt to 'trust the checks' would teach a release
manager to rubber-stamp, and a manufactured approval is worse than no
approval."* It then stated the consequence: **"a clean run promotes"**.

Measured on run 693, five days later, and it was never true. Both declared
checks passed with exit 0, the draft PR was created, and promotion was skipped.
`blocked` is set from the absent agent verdict BEFORE the checks are joined --
and they are joined with `or`, so a green check can only ever add a reason to
refuse, never remove one. The intent of #73 never reached the engine.

This closes that gap, and the whole safety of it lives in one distinction:

    "this pipeline declares no verdict-producing stage"   -- may promote
    "a verdict was expected here and did not arrive"      -- still blocks

The second is the fail-closed case defended everywhere else in this codebase,
and it stays. Only the first is new, it is OPT-IN per pipeline, and it is inert
unless the operator writes it down.

One more condition, because a gate that cannot refuse is not a gate: promotion
requires that checks were actually DECLARED and that every one of them passed.
`verdict_required: false` on a pipeline with no checks would promote on nothing
at all -- the vacuous gate this file exists to avoid.
"""

from __future__ import annotations

import pytest

from hivepilot.services.gate_policy import may_promote_without_verdict


class TestTheOptInIsRequired:
    def test_by_default_an_absent_verdict_still_blocks(self):
        """The polarity everything else in this codebase depends on. A
        pipeline that says nothing gets today's behaviour exactly."""
        assert not may_promote_without_verdict(
            verdict_required=True, checks_declared=2, checks_passed=2
        )

    def test_the_flag_alone_does_not_promote(self):
        """It removes the verdict requirement. It does not remove the need for
        evidence."""
        assert not may_promote_without_verdict(
            verdict_required=False, checks_declared=0, checks_passed=0
        )


class TestTheEvidenceMustExistAndBeGreen:
    def test_declared_and_all_passing_promotes(self):
        assert may_promote_without_verdict(
            verdict_required=False, checks_declared=2, checks_passed=2
        )

    def test_one_failing_check_blocks(self):
        assert not may_promote_without_verdict(
            verdict_required=False, checks_declared=2, checks_passed=1
        )

    def test_no_checks_at_all_blocks(self):
        """The vacuous gate. A pipeline that opted out of the verdict and
        declared nothing to run has removed its gate, not replaced it -- and it
        would promote every run, forever, in silence."""
        assert not may_promote_without_verdict(
            verdict_required=False, checks_declared=0, checks_passed=0
        )

    @pytest.mark.parametrize("declared,passed", [(1, 0), (3, 2), (5, 0)])
    def test_any_shortfall_blocks(self, declared, passed):
        assert not may_promote_without_verdict(
            verdict_required=False, checks_declared=declared, checks_passed=passed
        )

    def test_more_passes_than_declared_is_refused_not_trusted(self):
        """Not reachable today, and that is the point: it means the caller
        counted two different things. Refusing costs a manual promote;
        trusting it promotes on a number nobody can explain."""
        assert not may_promote_without_verdict(
            verdict_required=False, checks_declared=2, checks_passed=3
        )


class TestTheWiringInPerformGitActions:
    """The predicate is pure; these pin what the gate actually does with it.

    The case that matters is the one where the opt-out must NOT help: a red
    check has to keep blocking, or `verdict_required: false` becomes "promote
    anyway" and the pipeline has no gate at all.
    """

    @staticmethod
    def _promote(*, verdict_required, outcomes, task_result=None, verdict="absent"):
        """Drive the real gate arithmetic with a stubbed check run."""
        from unittest.mock import patch

        from hivepilot.models import GitActions, ProjectConfig
        from hivepilot.services import git_service
        from hivepilot.services.verification_service import (
            Check,
            CheckResult,
            VerificationReport,
        )

        report = VerificationReport(
            results=tuple(
                CheckResult(f"c{i}", o, 0 if o == "passed" else 1, "", 0.1)
                for i, o in enumerate(outcomes)
            )
        )
        promoted: list[str] = []

        with (
            patch.object(git_service, "ensure_repo"),
            patch.object(git_service, "checkout_for_reading"),
            patch.object(git_service, "run_checks", return_value=report),
            patch.object(git_service, "create_pr"),
            patch.object(git_service, "post_blocked_report"),
            patch.object(git_service, "promote_pr", side_effect=lambda **kw: promoted.append("y")),
            patch.object(git_service, "_ci_probe_for", return_value=None),
        ):
            git_service.perform_git_actions(
                project_name="p",
                project=ProjectConfig(path=__import__("pathlib").Path("/tmp")),
                git=GitActions(commit=False, push=False, create_pr=False, promote_pr=True),
                # forage's real shape, corrected after this test caught my
                # first version: `_agent_verdict_blocked` is a BLOCKLIST, so a
                # None task_result does not block. Run 693 was blocked by the
                # judge gate on an ABSENT verdict, which is what this models.
                task_result=task_result,
                judge_gate_enabled=True,
                verdict=None if verdict == "absent" else verdict,
                checks=[
                    Check(name=f"c{i}", command="true", timeout_seconds=10)
                    for i in range(len(outcomes))
                ],
                verdict_required=verdict_required,
            )
        return bool(promoted)

    def test_the_default_still_blocks_a_verdictless_run(self):
        """What run 693 measured, and what must not change for any pipeline
        that did not opt out."""
        assert not self._promote(verdict_required=True, outcomes=["passed", "passed"])

    def test_opted_out_with_green_checks_promotes(self):
        """Config PR #73's stated consequence, finally true."""
        assert self._promote(verdict_required=False, outcomes=["passed", "passed"])

    def test_opted_out_with_a_red_check_still_blocks(self):
        """The discriminating case. If this ever passes, `verdict_required:
        false` has stopped meaning "the checks are my gate" and started
        meaning "promote anyway"."""
        assert not self._promote(verdict_required=False, outcomes=["passed", "failed"])

    def test_opted_out_with_an_errored_check_still_blocks(self):
        """`errored` is not `failed` and is not `passed`. A check that could
        not run verified nothing."""
        assert not self._promote(verdict_required=False, outcomes=["errored"])

    def test_an_agent_that_refused_is_never_overruled(self):
        """The fail-open my first implementation had, caught by the test above
        it. An explicit blocking `status:` is a DECISION, not an absence, and
        no pipeline setting may lift it -- otherwise `verdict_required: false`
        silently overrules every role that can say no."""
        assert not self._promote(
            verdict_required=False,
            outcomes=["passed", "passed"],
            task_result="status: BLOCKED\nthe change ships a secret",
        )

    def test_a_verdict_that_exists_but_refuses_is_never_overruled(self):
        """`verdict is None` is required. A judge verdict that declined, or
        approved too weakly, is an answer -- and an answer is not an absence."""
        from hivepilot.orchestrator import Verdict

        assert not self._promote(
            verdict_required=False,
            outcomes=["passed", "passed"],
            verdict=Verdict(decision="BLOCKED", confidence=1.0),
        )
