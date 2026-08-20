"""The autonomy ladder has never had a measurable input.

Verdicts (adversarial review) and approvals (the destructive-step gate) have
never occurred on the same run — 10 approvals, 0 paired verdicts. The human
decision that DOES pair with a gate outcome is what happens to the pull request
afterwards, and nothing records it.

FOUR cases, not two. The task described only the blocked ones — you merge
(override) or you close (agreement) — and that measures the gate in one
direction only: how often it is too strict. The case it leaves out is the
expensive one:

    gate PROMOTED, human CLOSED  -- the gate approved something you rejected.

A ladder blind to that learns only to loosen. So all four are recorded, and the
pair (gate, human) is kept rather than collapsed into "agree/disagree" — the
two disagreements are not the same event and averaging them would hide which
direction the gate is wrong in.

Two refusals matter as much as the four records:

    an OPEN pull request is not a decision. Absent from the measure, honestly,
    rather than counted as a weak agreement;

    an actor we cannot tell apart from HivePilot's own token is never recorded.
    `git.merge_pr` exists — the engine can merge its own work — and feeding
    that back as a "human decision" would close the loop on itself. That is
    the plausible-zero shape this whole codebase keeps being bitten by.
"""

from __future__ import annotations

import pytest

from hivepilot.services.pr_decision import classify_pr_decision


def _classify(**kw):
    base = {
        "gate_blocked": True,
        "pr_state": "MERGED",
        "actor": "jeromesoyer",
        "engine_actor": "hivepilot-bot",
    }
    base.update(kw)
    return classify_pr_decision(**base)


class TestTheFourCases:
    def test_blocked_then_merged_is_an_override(self):
        """You disagreed with the block and shipped anyway."""
        assert _classify(gate_blocked=True, pr_state="MERGED")["decision"] == "override"

    def test_blocked_then_closed_is_agreement(self):
        assert _classify(gate_blocked=True, pr_state="CLOSED")["decision"] == "agreed"

    def test_promoted_then_merged_is_agreement(self):
        assert _classify(gate_blocked=False, pr_state="MERGED")["decision"] == "agreed"

    def test_promoted_then_closed_is_the_expensive_override(self):
        """The case the task left out, and the one worth having: the gate
        approved something you rejected. A ladder blind to it learns only to
        loosen."""
        assert _classify(gate_blocked=False, pr_state="CLOSED")["decision"] == "override"

    def test_the_pair_is_kept_not_collapsed(self):
        """Both overrides are "override", but they are opposite failures. The
        row must still say which way the gate was wrong."""
        too_strict = _classify(gate_blocked=True, pr_state="MERGED")
        too_loose = _classify(gate_blocked=False, pr_state="CLOSED")

        assert too_strict["decision"] == too_loose["decision"] == "override"
        assert too_strict["gate_blocked"] is True
        assert too_loose["gate_blocked"] is False


class TestAnOpenPullRequestIsNotADecision:
    def test_open_records_nothing(self):
        """Not a weak agreement, not a pending override. Nobody has decided."""
        assert _classify(pr_state="OPEN") is None

    @pytest.mark.parametrize("state", ["", None, "DRAFT", "weird"])
    def test_an_unrecognised_state_records_nothing_either(self, state):
        """A state this cannot read is not evidence of anything, and guessing
        would put a fabricated row into the one table the ladder trusts."""
        assert _classify(pr_state=state) is None


class TestTheActorMustBeAHuman:
    def test_the_engine_merging_its_own_work_is_not_a_human_decision(self):
        """`git.merge_pr` exists. Recording that would feed the ladder its own
        output and close the loop on itself."""
        assert _classify(actor="hivepilot-bot") is None

    def test_the_comparison_ignores_case_and_surrounding_space(self):
        """A configured account name and what the forge reports differ in
        shape often enough that an exact match would let the engine through."""
        assert _classify(actor="  HivePilot-Bot  ") is None

    def test_an_unknown_actor_on_a_MERGE_records_nothing(self):
        """Refusing to attribute is the honest answer. Defaulting to "human"
        would silently count every automated merge as a decision."""
        assert _classify(actor=None, pr_state="MERGED") is None
        assert _classify(actor="", pr_state="MERGED") is None

    def test_an_unknown_actor_on_a_CLOSE_is_still_recorded(self):
        """Asymmetric on purpose, and grounded: the engine can merge but has
        no close path, so a closed pull request is necessarily a person's
        doing. `gh pr view` exposes `mergedBy` and no equivalent for a close,
        so refusing these would drop two of the four cases -- including the
        expensive one."""
        result = _classify(actor=None, pr_state="CLOSED", gate_blocked=False)

        assert result is not None
        assert result["decision"] == "override"
        assert result["actor"] == "unknown"

    def test_the_engine_still_cannot_own_a_close_it_did_name(self):
        """The asymmetry relaxes an UNNAMED actor, never a named engine one."""
        assert _classify(actor="hivepilot-bot", pr_state="CLOSED") is None

    def test_no_engine_actor_configured_still_records_a_named_human(self):
        """A deployment that never merges automatically has nothing to
        exclude, and must not lose every decision because of it."""
        result = _classify(actor="jeromesoyer", engine_actor=None)

        assert result is not None
        assert result["actor"] == "jeromesoyer"

    def test_a_named_human_is_recorded_with_their_name(self):
        """So a ladder built on this can tell one operator's overrides from
        another's."""
        assert _classify(actor="someone-else")["actor"] == "someone-else"


class TestATruthyNonBoolDoesNotInvertIt:
    def test_a_truthy_string_is_read_as_blocked(self):
        """`is not` compares identity and only works for the True/False
        singletons: any other truthy value would have flipped the verdict
        silently rather than failing."""
        result = classify_pr_decision(
            gate_blocked="yes", pr_state="MERGED", actor="me", engine_actor=None
        )

        assert result["decision"] == "override"
        assert result["gate_blocked"] is True

    def test_a_falsy_value_is_read_as_not_blocked(self):
        result = classify_pr_decision(
            gate_blocked=0, pr_state="MERGED", actor="me", engine_actor=None
        )

        assert result["decision"] == "agreed"
        assert result["gate_blocked"] is False


class TestThePremiseBehindTheAsymmetry:
    """Unnamed CLOSED decisions are accepted because the engine cannot close a
    pull request. If that ever stops being true, this must fail loudly rather
    than let automated closures be counted as human judgement."""

    def test_the_engine_has_no_close_path(self):
        import inspect

        from hivepilot.forges import github as gh_forge

        source = inspect.getsource(gh_forge)
        assert '"pr", "close"' not in source
        assert "pr close" not in source

    def test_the_engine_does_have_a_merge_path(self):
        """The other half of the premise: merges DO need the actor check,
        because this exists."""
        import inspect

        from hivepilot.forges import github as gh_forge

        assert '"pr", "merge"' in inspect.getsource(gh_forge)
