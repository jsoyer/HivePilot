"""A step that crashed is the highest-signal thing a run produces.

The distiller feeds on verdicts and interactions — *judgements about
completed work*. A step that dies produces neither, so the failures that
actually cost money teach nothing.

Three from this week, each expensive, none of which left a lesson behind:

    reviewer   allowed_tools silently replaced   → API 400        $0.56
    CTO        denied `rtk fd`, explored by hand → "too long"     $1.17
    CTO        (again, before the grant landed)  → same           $1.17

Every one was a one-line configuration gap whose diagnosis took reading a
4 000-character failure blob by hand. That is precisely the shape of thing a
lesson is for: cheap to state, expensive to rediscover.

Two changes make them visible to the distiller.

**Failures are passed as their own signal.** `outcomes` carried only the
run-level verdict — one dict, success true or false. Which step died, in
which role, with what error, was never in the material the distiller saw.

**A failure alone triggers distillation.** The entry condition was "no
verdicts AND no interactions → skip", on the reasoning that an outcome-only
run is not worth a costed call. A run that only *failed* is the one where a
lesson is worth most, and it is exactly the run that produces no verdict.
"""

from __future__ import annotations

from hivepilot.services.lessons_service import build_distill_prompt, has_distillable_signal

_FAILURE = {
    "step": "cto review",
    "role": "cto",
    "status": "failed",
    "detail": "claude exited 1: Prompt is too long (denied: rtk fd . surfaces/agent)",
}


class TestAFailureReachesThePrompt:
    def test_the_failed_step_is_named(self) -> None:
        prompt = build_distill_prompt(
            project="noxys",
            role=None,
            task="t",
            verdicts=[],
            interactions=[],
            outcomes=[],
            failures=[_FAILURE],
        )

        assert "cto review" in prompt

    def test_the_error_is_carried(self) -> None:
        """The step name alone says something broke. The error says WHAT,
        which is the only part a lesson can be written from."""
        prompt = build_distill_prompt(
            project="noxys",
            role=None,
            task="t",
            verdicts=[],
            interactions=[],
            outcomes=[],
            failures=[_FAILURE],
        )

        assert "Prompt is too long" in prompt

    def test_the_role_is_carried(self) -> None:
        """ "A step failed" is not actionable; "the CTO failed this way" is —
        the fix lands in that role's configuration."""
        prompt = build_distill_prompt(
            project="noxys",
            role=None,
            task="t",
            verdicts=[],
            interactions=[],
            outcomes=[],
            failures=[_FAILURE],
        )

        assert "cto" in prompt

    def test_no_failures_adds_nothing(self) -> None:
        """A clean run must render exactly as before — no empty section
        inviting the model to invent one."""
        clean = build_distill_prompt(
            project="p",
            role=None,
            task="t",
            verdicts=[],
            interactions=[],
            outcomes=[],
            failures=[],
        )

        assert "cto review" not in clean


class TestAFailureIsEnoughToTrigger:
    def test_a_failure_alone_is_distillable(self) -> None:
        """The run that only failed is the one where a lesson is worth most,
        and it is exactly the run that produces no verdict."""
        assert has_distillable_signal(verdicts=[], interactions=[], failures=[_FAILURE])

    def test_verdicts_alone_still_trigger(self) -> None:
        assert has_distillable_signal(verdicts=[{"x": 1}], interactions=[], failures=[])

    def test_interactions_alone_still_trigger(self) -> None:
        assert has_distillable_signal(verdicts=[], interactions=[{"x": 1}], failures=[])

    def test_nothing_at_all_still_skips(self) -> None:
        """A clean, quiet run is not worth a costed LLM call — that
        reasoning was right and survives."""
        assert not has_distillable_signal(verdicts=[], interactions=[], failures=[])
