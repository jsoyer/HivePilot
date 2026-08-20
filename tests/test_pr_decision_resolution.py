"""The recording half of the ladder's first measurable input.

`classify_pr_decision` decides; this observes. The tests here exist because the
observing half is almost entirely `except` branches, and those are the branches
this codebase keeps shipping broken -- three NameErrors in `except` blocks in a
single session, each one invisible until the failure they handled occurred.

The discriminating case is the engine-actor lookup. `viewer_login` returns None
for BOTH "this deployment never merges automatically" and "the forge would not
answer", and only the first is safe to treat as an empty exclusion. Passing an
unknown-because-unreadable None into the classifier would record the engine's
own merges as human decisions -- closing the ladder's loop on its own output,
which is the exact shape this whole measurement exists to avoid.

So the caller defers instead. Rows are never rewritten once decided, so a
deferred row costs nothing: the next run with a working forge resolves it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hivepilot.services import pr_decision


class _Forge:
    """A forge that answers from a script, and counts what it was asked."""

    def __init__(self, *, viewer="hivepilot-bot", outcomes=None, viewer_raises=None):
        self._viewer = viewer
        self._viewer_raises = viewer_raises
        self._outcomes = outcomes or {}
        self.observed: list[str] = []

    def viewer_login(self, *, project):
        if self._viewer_raises:
            raise self._viewer_raises
        return self._viewer

    def pr_outcome(self, *, project, branch):
        self.observed.append(branch)
        result = self._outcomes.get(branch, ("OPEN", None))
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def recorded():
    """Capture `resolve_pr_gate_outcome` calls without a database."""
    calls: list[dict] = []

    def _resolve(**kw):
        calls.append(kw)

    with patch("hivepilot.services.state_service.resolve_pr_gate_outcome", _resolve):
        yield calls


def _pending(*rows):
    return patch(
        "hivepilot.services.state_service.unresolved_pr_gate_outcomes", lambda **_: list(rows)
    )


class TestItRecordsTheDecision:
    def test_a_merged_pr_the_gate_blocked_is_recorded_as_an_override(self, recorded):
        forge = _Forge(outcomes={"hivepilot/p/1": ("MERGED", "jeromesoyer")})

        with _pending({"branch": "hivepilot/p/1", "gate_blocked": 1}):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=forge
            )

        assert count == 1
        assert recorded == [
            {
                "branch": "hivepilot/p/1",
                "decision": "override",
                "pr_state": "MERGED",
                "actor": "jeromesoyer",
            }
        ]

    def test_the_expensive_case_survives_the_round_trip(self, recorded):
        """gate promoted + human closed. The one the original task left out,
        and the only evidence the gate is too LOOSE."""
        forge = _Forge(outcomes={"b": ("CLOSED", None)})

        with _pending({"branch": "b", "gate_blocked": 0}):
            pr_decision.resolve_open_decisions(project_name="p", project=object(), forge=forge)

        assert recorded[0]["decision"] == "override"
        assert recorded[0]["actor"] == "unknown"

    def test_an_open_pr_is_observed_but_not_recorded(self, recorded):
        """It stays an open question rather than becoming a weak agreement."""
        forge = _Forge(outcomes={"b": ("OPEN", None)})

        with _pending({"branch": "b", "gate_blocked": 1}):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=forge
            )

        assert forge.observed == ["b"]
        assert count == 0
        assert recorded == []

    def test_the_engines_own_merge_is_observed_but_not_recorded(self, recorded):
        forge = _Forge(viewer="hivepilot-bot", outcomes={"b": ("MERGED", "hivepilot-bot")})

        with _pending({"branch": "b", "gate_blocked": 1}):
            pr_decision.resolve_open_decisions(project_name="p", project=object(), forge=forge)

        assert recorded == []


class TestAnUnknownEngineActorDefersEverything:
    """The point of the module. None from `viewer_login` is ambiguous, and
    only one of its two meanings is safe."""

    @pytest.mark.parametrize("viewer", [None, "", "   "])
    def test_it_records_nothing_and_does_not_even_look(self, recorded, viewer):
        """Not "records nothing this time" by luck -- it must not reach the
        forge at all, because every answer it could get is unusable."""
        forge = _Forge(viewer=viewer, outcomes={"b": ("MERGED", "jeromesoyer")})

        with _pending({"branch": "b", "gate_blocked": 1}):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=forge
            )

        assert count == 0
        assert recorded == []
        assert forge.observed == [], "observed pull requests it could not classify"

    def test_a_raising_lookup_defers_too_rather_than_propagating(self, recorded):
        forge = _Forge(viewer_raises=RuntimeError("gh: not authenticated"))

        with _pending({"branch": "b", "gate_blocked": 1}):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=forge
            )

        assert count == 0
        assert recorded == []

    def test_a_deferred_row_is_resolved_by_the_next_working_run(self, recorded):
        """Deferring is only free because the row survives. Same row, same
        forge answer, a working lookup the second time."""
        outcomes = {"b": ("MERGED", "jeromesoyer")}
        row = {"branch": "b", "gate_blocked": 1}

        with _pending(row):
            pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=_Forge(viewer=None, outcomes=outcomes)
            )
            assert recorded == []

            pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=_Forge(outcomes=outcomes)
            )

        assert [c["decision"] for c in recorded] == ["override"]


class TestTheExceptBranchesActuallyRun:
    """Each of these executes a branch that only runs when something breaks.
    Written because three `except` blocks in this codebase raised NameError on
    first contact -- the handler had never been executed by anything."""

    def test_an_unlistable_ledger_returns_zero(self, recorded):
        with patch(
            "hivepilot.services.state_service.unresolved_pr_gate_outcomes",
            side_effect=RuntimeError("no such table"),
        ):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=_Forge()
            )

        assert count == 0

    def test_one_unobservable_pr_does_not_stop_the_others(self, recorded):
        forge = _Forge(
            outcomes={
                "bad": RuntimeError("network"),
                "good": ("MERGED", "jeromesoyer"),
            }
        )

        with _pending({"branch": "bad", "gate_blocked": 1}, {"branch": "good", "gate_blocked": 1}):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=forge
            )

        assert count == 1
        assert [c["branch"] for c in recorded] == ["good"]

    def test_one_unwritable_row_does_not_stop_the_others(self):
        written: list[str] = []

        def _resolve(**kw):
            if kw["branch"] == "bad":
                raise RuntimeError("database is locked")
            written.append(kw["branch"])

        forge = _Forge(
            outcomes={"bad": ("MERGED", "jeromesoyer"), "good": ("MERGED", "jeromesoyer")}
        )
        with (
            _pending({"branch": "bad", "gate_blocked": 1}, {"branch": "good", "gate_blocked": 1}),
            patch("hivepilot.services.state_service.resolve_pr_gate_outcome", _resolve),
        ):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=forge
            )

        assert written == ["good"]
        assert count == 1, "counted a row it failed to write"

    def test_a_row_without_a_branch_is_skipped_not_crashed_on(self, recorded):
        forge = _Forge(outcomes={"good": ("MERGED", "jeromesoyer")})

        with _pending({"branch": None, "gate_blocked": 1}, {"branch": "good", "gate_blocked": 1}):
            count = pr_decision.resolve_open_decisions(
                project_name="p", project=object(), forge=forge
            )

        assert count == 1
        assert forge.observed == ["good"]


class TestTheGithubForgeAnswersInThatShape:
    """The resolver is tested against a fake, so the real forge's two methods
    are pinned separately -- otherwise both halves could agree with each other
    and disagree with `gh`."""

    def test_pr_outcome_reads_state_and_the_merging_actor(self):
        from hivepilot.forges.github import GitHubForge

        class _Done:
            stdout = '{"state": "MERGED", "mergedBy": {"login": "jeromesoyer"}}'

        with patch("hivepilot.forges.github.subprocess.run", return_value=_Done()):
            state, actor = GitHubForge().pr_outcome(project=_Proj(), branch="b")

        assert (state, actor) == ("MERGED", "jeromesoyer")

    def test_a_close_reports_no_actor_rather_than_an_empty_dict(self):
        """`gh` returns `mergedBy: null` for a close. That must arrive as None
        -- `{}` or `""` would be a name the classifier compares against the
        engine's login."""
        from hivepilot.forges.github import GitHubForge

        class _Done:
            stdout = '{"state": "CLOSED", "mergedBy": null}'

        with patch("hivepilot.forges.github.subprocess.run", return_value=_Done()):
            assert GitHubForge().pr_outcome(project=_Proj(), branch="b") == ("CLOSED", None)

    @pytest.mark.parametrize("stdout", ["", "not json", "{}"])
    def test_an_unreadable_answer_is_empty_not_an_exception(self, stdout):
        """It runs inside somebody else's run. `("", None)` classifies to None,
        which leaves the row unresolved -- the honest outcome."""
        from hivepilot.forges.github import GitHubForge

        class _Done:
            pass

        _Done.stdout = stdout
        with patch("hivepilot.forges.github.subprocess.run", return_value=_Done()):
            assert GitHubForge().pr_outcome(project=_Proj(), branch="b") == ("", None)

    def test_viewer_login_is_asked_of_gh_not_read_from_config(self):
        """Grounded rather than configured: the account `gh` is authenticated
        as IS the one `gh pr merge` would act under, so there is no second
        value to drift out of step."""
        from hivepilot.forges.github import GitHubForge

        class _Done:
            stdout = "hivepilot-bot\n"

        with patch("hivepilot.forges.github.subprocess.run", return_value=_Done()) as run:
            assert GitHubForge().viewer_login(project=_Proj()) == "hivepilot-bot"

        assert run.call_args[0][0][1:3] == ["api", "user"]

    def test_an_unauthenticated_gh_yields_none(self):
        from hivepilot.forges.github import GitHubForge

        with patch("hivepilot.forges.github.subprocess.run", side_effect=OSError("no gh")):
            assert GitHubForge().viewer_login(project=_Proj()) is None


class _Proj:
    path = "/tmp"
    name = "p"
