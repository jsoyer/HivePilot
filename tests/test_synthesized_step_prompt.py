"""An internal call carries its whole prompt; it has no role file to read.

The claude runner requires `step.prompt_file` and raises without it. That is
right for a ROLE step: a reviewer with no prompt file is a misconfiguration,
and failing loudly beats dispatching an agent with no instructions.

It is wrong for the engine's own synthesized calls. Three of them build a
step with `prompt_file=None` deliberately and put the entire prompt in
`metadata["extra_prompt"]` — there is no role, no persona, no file to point
at:

    orchestrator.py:5144   `{role}-judge`
    orchestrator.py:5208   the arbiter
    lessons_service.py:346 `lessons-distiller`

So all three were unreachable through the claude runner. The distiller is
the one that shows: every pipeline run now logs

    lessons.distill_error: Step 'lessons-distiller' requires a prompt_file

and `lessons` has been 0 since the table existed, across 136 interactions
and 15 verdicts. Wiring the pipeline into the loop (#441) did not create
this — it revealed it. Before that the call simply never happened, so the
failure had nothing to surface from.

The rule that distinguishes the two cases is not "who is calling" but "is
there a prompt at all". A step with neither a file nor an `extra_prompt` is
broken however it was built, and still raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner


def _runner() -> ClaudeRunner:
    return ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command=None), settings)


def _payload(tmp_path: Path, *, prompt_file: str | None, **metadata) -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="lessons-distiller", runner="claude", prompt_file=prompt_file),
        metadata=dict(metadata),
        secrets={},
    )


class TestASynthesizedCallGetsThrough:
    def test_no_prompt_file_but_an_extra_prompt_is_accepted(self, tmp_path) -> None:
        """The distiller's exact shape: whole prompt in `extra_prompt`, no
        role file, because there is no role."""
        prompt = (
            _runner()._build_invocation(
                _payload(tmp_path, prompt_file=None, extra_prompt="DISTILL THIS")
            )[1]
            is not None
        )

        assert prompt

    def test_the_supplied_prompt_actually_reaches_the_agent(self, tmp_path) -> None:
        """Accepting the call is not enough — the text has to arrive. A
        dispatch that runs with an empty prompt would be worse than the
        error it replaces."""
        args = _runner()._build_invocation(
            _payload(tmp_path, prompt_file=None, extra_prompt="DISTILL THIS")
        )[0]

        assert any("DISTILL THIS" in a for a in args)


class TestARoleStepStillFailsLoudly:
    def test_neither_file_nor_prompt_raises(self, tmp_path) -> None:
        """A reviewer with no prompt file is a misconfiguration. Dispatching
        an agent with no instructions at all is the failure this error
        exists to prevent, and it must survive."""
        with pytest.raises(ValueError, match="prompt"):
            _runner()._build_invocation(_payload(tmp_path, prompt_file=None))

    def test_an_empty_extra_prompt_is_not_a_prompt(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="prompt"):
            _runner()._build_invocation(_payload(tmp_path, prompt_file=None, extra_prompt="   "))


class TestTheNormalPathIsUnchanged:
    def test_a_prompt_file_still_works(self, tmp_path) -> None:
        f = tmp_path / "role.md"
        f.write_text("ROLE INSTRUCTIONS", encoding="utf-8")

        args = _runner()._build_invocation(_payload(tmp_path, prompt_file=str(f)))[0]

        assert any("ROLE INSTRUCTIONS" in a for a in args)

    def test_a_prompt_file_wins_when_both_are_present(self, tmp_path) -> None:
        """`extra_prompt` has always been additive to a role file, never a
        replacement. Both must still appear."""
        f = tmp_path / "role.md"
        f.write_text("ROLE INSTRUCTIONS", encoding="utf-8")

        args = _runner()._build_invocation(
            _payload(tmp_path, prompt_file=str(f), extra_prompt="EXTRA")
        )[0]

        joined = " ".join(args)
        assert "ROLE INSTRUCTIONS" in joined
        assert "EXTRA" in joined
