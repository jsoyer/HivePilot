"""Who runs is an authorisation decision wearing a cost-saving hat.

The motivation is real and measured: **$5.94 for a 100-line stdlib script**,
ten stages including a CISO and a QA pass on a CLI tool with no network, no
credentials and no dependencies. Letting the Chief of Staff propose a smaller
team is worth doing.

But "skip the security review" is exactly the shape of decision that must fail
CLOSED, and every guard here exists because the failure mode is silent. A
composition that quietly drops the CISO produces a green pipeline, a merged PR,
and no artefact anywhere saying a review was skipped -- indistinguishable from
a review that passed.

So:

* **Select, never invent.** A name the agent made up is ignored and recorded,
  never resolved to something plausible.
* **Blocking roles are not removable.** `can_block` has been descriptive
  metadata read nowhere in the execution path; this is the first thing that
  enforces it, and it enforces it *against* the agent.
* **Fail closed means the FULL team.** Absent, empty or unparseable output runs
  everything. The expensive default is the safe one, and an agent that emits
  nothing must not thereby empty the roster.
* **Recorded with a reason.** A decision nobody can audit afterwards is
  indistinguishable from no decision.
"""

from __future__ import annotations

import pytest

from hivepilot.services.team_composition import (
    StageFacts,
    decide,
    parse_team_directive,
)


def _stage(name, role=None, *, can_block=False, release_gate=False):
    return StageFacts(name=name, role=role, can_block=can_block, is_release_gate=release_gate)


@pytest.fixture
def roster():
    """A greenfield pipeline, in order."""
    return [
        _stage("CEO Intake", "ceo"),
        _stage("Product Spec", "pm"),
        _stage("Plan Synthesis", "chief-of-staff"),
        _stage("CTO", "cto"),
        _stage("Implementation", "developer"),
        _stage("Review", "reviewer", can_block=True),
        _stage("Security", "ciso", can_block=True),
        _stage("QA", "qa", can_block=True),
        _stage("Documentation", "documentation"),
        _stage("PR Approval", "release-manager", release_gate=True),
    ]


class TestTheDirectiveIsParsedTolerantly:
    def test_a_comma_separated_line(self):
        assert parse_team_directive("TEAM: reviewer, ciso, qa") == ["reviewer", "ciso", "qa"]

    def test_it_is_found_among_other_prose(self):
        text = "Here is my plan.\n\nTEAM: developer, reviewer\n\nRationale: small tool."
        assert parse_team_directive(text) == ["developer", "reviewer"]

    def test_case_and_spacing_do_not_matter(self):
        assert parse_team_directive("team:  Reviewer ,  CISO ") == ["reviewer", "ciso"]

    def test_absent_directive_is_none_not_empty(self):
        """None means "said nothing"; [] would mean "chose nobody", and the two
        must not collapse -- one runs everything, the other would run nothing."""
        assert parse_team_directive("A plan with no directive at all.") is None
        assert parse_team_directive(None) is None
        assert parse_team_directive("") is None

    def test_an_empty_directive_is_empty_not_none(self):
        assert parse_team_directive("TEAM:") == []


class TestFailClosedRunsEverything:
    def test_no_directive_runs_the_whole_roster(self, roster):
        decision = decide(stages=roster, selector_output="no directive here")

        assert decision.applied is False
        assert decision.dropped == ()
        assert len(decision.selected) == len(roster)

    def test_an_empty_selection_runs_the_whole_roster(self, roster):
        """An agent that names nobody must not thereby empty the roster."""
        decision = decide(stages=roster, selector_output="TEAM:")

        assert decision.applied is False
        assert decision.dropped == ()

    def test_a_wholly_invented_selection_runs_the_whole_roster(self, roster):
        decision = decide(stages=roster, selector_output="TEAM: wizard, oracle")

        assert decision.applied is False
        assert set(decision.unknown) == {"wizard", "oracle"}
        assert decision.dropped == ()


class TestSelectNeverInvent:
    def test_an_unknown_name_is_ignored_and_recorded(self, roster):
        decision = decide(stages=roster, selector_output="TEAM: developer, wizard")

        assert "wizard" in decision.unknown
        assert "Implementation" in decision.selected

    def test_an_unknown_name_never_resolves_to_something_plausible(self, roster):
        """'review' is not 'reviewer'. Near-misses are the dangerous case: a
        fuzzy match would silently pick a role the agent did not name."""
        decision = decide(stages=roster, selector_output="TEAM: developer, review")

        assert "review" in decision.unknown


class TestBlockingRolesSurvive:
    def test_a_can_block_role_is_restored_when_dropped(self, roster):
        decision = decide(stages=roster, selector_output="TEAM: developer")

        assert "Security" in decision.selected
        assert "Review" in decision.selected
        assert "QA" in decision.selected
        assert set(decision.kept_despite) >= {"Review", "Security", "QA"}

    def test_the_release_gate_is_restored_when_dropped(self, roster):
        decision = decide(stages=roster, selector_output="TEAM: developer")

        assert "PR Approval" in decision.selected
        assert "PR Approval" in decision.kept_despite

    def test_a_non_blocking_role_really_is_dropped(self, roster):
        """The feature has to actually save something, or it is theatre."""
        decision = decide(stages=roster, selector_output="TEAM: developer")

        assert decision.applied is True
        assert "Documentation" in decision.dropped
        assert "CEO Intake" in decision.dropped

    def test_the_selector_stage_itself_is_never_dropped(self, roster):
        """It has already run by the time its own output is read; dropping it
        retroactively would make the record incoherent."""
        decision = decide(
            stages=roster, selector_output="TEAM: developer", selector_stage="Plan Synthesis"
        )

        assert "Plan Synthesis" in decision.selected


class TestTheDecisionIsAuditable:
    def test_a_reason_is_always_present(self, roster):
        for output in ("TEAM: developer", "TEAM:", "nothing", None):
            assert decide(stages=roster, selector_output=output).reason

    def test_the_reason_names_what_was_restored(self, roster):
        reason = decide(stages=roster, selector_output="TEAM: developer").reason

        assert "Security" in reason or "can_block" in reason

    def test_the_reason_names_the_invented_names(self, roster):
        reason = decide(stages=roster, selector_output="TEAM: developer, wizard").reason

        assert "wizard" in reason

    def test_order_is_preserved(self, roster):
        """Stages run in declared order; a decision that reorders them would
        change the pipeline's meaning, not just its size."""
        decision = decide(stages=roster, selector_output="TEAM: documentation, developer")

        assert list(decision.selected).index("Implementation") < list(decision.selected).index(
            "Documentation"
        )


class TestAStageWithNoRole:
    def test_it_is_kept(self, roster):
        """A stage the selector cannot name by role cannot be consented to
        either, so it is never dropped by a selection that does not mention it."""
        stages = [*roster, _stage("Housekeeping", None)]

        decision = decide(stages=stages, selector_output="TEAM: developer")

        assert "Housekeeping" in decision.selected
