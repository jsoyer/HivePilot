"""The lessons distiller has never produced a lesson, and this is why.

Run 390 — the first full pipeline to execute after the three wiring fixes
(#441/#442/#443) landed — reached the distiller and died there:

    lessons.distill_error: [Errno 7] Argument list too long: 'claude'

Not a wiring problem, not the flag: `enable_lesson_distillation` is true,
the call fires at pipeline end, the code runs. It dies on an operating
system limit. Linux caps a *single* argv element at `MAX_ARG_STRLEN`
(131 072 bytes) regardless of the much larger total `ARG_MAX`, and the
runner passes the whole prompt as one argument.

The prompt got there because every block interpolates its rows whole:

    f"- id={i.get('id')} actor={i.get('actor')} ... summary={i.get('summary')!r}"

`summary` is a stage's entire output. Run 390's twelve interactions carry
111 797 bytes between them — about 9 KB each — and the table holds 203 rows
totalling 1 035 359 bytes. Nothing bounded any of it.

Passing the prompt on stdin instead would only move the failure: a 112 KB
prompt sent to opus exceeds the context window and comes back as "Prompt is
too long", which is the CTO's $1.17 failure mode, except billed. The input
is the thing that is wrong. A distiller handed 112 KB of prose does not
distil it, it paraphrases it.

So: bound each field, bound each block, bound the whole, and say out loud
when trimming happens — a distiller that silently drops the material it was
supposed to learn from is the same defect one level up.
"""

from __future__ import annotations

import pytest

from hivepilot.services.lessons_service import (
    _MAX_ENTRIES_PER_BLOCK,
    _MAX_FIELD_CHARS,
    _MAX_PROMPT_CHARS,
    build_distill_prompt,
)

# Linux MAX_ARG_STRLEN. The prompt travels as one argv element, so this is
# the wall, not the (much larger) total ARG_MAX.
_MAX_ARG_STRLEN = 131_072


def _interaction(i: int, size: int) -> dict[str, object]:
    return {
        "id": i,
        "actor": f"actor-{i}",
        "action": "completed stage",
        "target": "noxys",
        "summary": f"START-{i}-" + ("x" * size) + f"-END-{i}",
    }


def _prompt(**over: object) -> str:
    base: dict[str, object] = {
        "project": "noxys",
        "role": None,
        "task": "t",
        "verdicts": [],
        "interactions": [],
        "outcomes": [],
        "failures": [],
    }
    base.update(over)
    return build_distill_prompt(**base)  # type: ignore[arg-type]


class TestTheRealShapeFits:
    def test_run_390s_material_fits_in_one_argv_element(self) -> None:
        """Twelve interactions of ~9 KB each — the exact shape that killed
        run 390's distillation."""
        out = _prompt(interactions=[_interaction(i, 9_000) for i in range(12)])

        assert len(out.encode()) < _MAX_ARG_STRLEN

    def test_the_whole_history_would_also_fit(self) -> None:
        """203 rows, 1 MB. A bound that only survives today's run is not a
        bound."""
        out = _prompt(interactions=[_interaction(i, 5_000) for i in range(203)])

        assert len(out.encode()) < _MAX_ARG_STRLEN

    def test_every_block_at_once_still_fits(self) -> None:
        fat = "y" * 20_000
        out = _prompt(
            verdicts=[{"id": n, "kind": "review", "summary": fat} for n in range(50)],
            interactions=[_interaction(n, 20_000) for n in range(50)],
            outcomes=[{"project": "p", "detail": fat} for _ in range(50)],
            failures=[{"step": "s", "role": "cto", "detail": fat} for _ in range(50)],
        )

        assert len(out.encode()) < _MAX_ARG_STRLEN


class TestTruncationIsVisible:
    def test_a_long_field_is_cut_and_says_so(self) -> None:
        out = _prompt(interactions=[_interaction(1, 50_000)])

        assert "START-1-" in out, "the head of the field must survive"
        assert "-END-1" not in out, "the tail must be gone"
        assert "truncated" in out.lower()

    def test_dropped_entries_are_announced_with_a_count(self) -> None:
        """Silently keeping 40 of 203 rows would let the distiller — and
        whoever reads its output — believe it saw the whole run."""
        out = _prompt(interactions=[_interaction(i, 100) for i in range(203)])

        assert f"{203 - _MAX_ENTRIES_PER_BLOCK}" in out
        assert "omitted" in out.lower()

    def test_the_most_recent_entries_are_the_ones_kept(self) -> None:
        """A lesson is written about what just happened. Keeping the oldest
        rows would distil the start of the run and drop the failure at
        its end."""
        out = _prompt(interactions=[_interaction(i, 100) for i in range(203)])

        assert "actor-202" in out
        assert "actor-0 " not in out


class TestNothingChangesForOrdinaryRuns:
    def test_a_small_run_is_not_annotated(self) -> None:
        """The common case must render exactly as before — no truncation
        notes, no omission counts, nothing inviting the model to wonder what
        it is missing."""
        out = _prompt(interactions=[_interaction(1, 50)])

        assert "truncated" not in out.lower()
        assert "omitted" not in out.lower()
        assert "-END-1" in out

    def test_an_empty_run_still_renders_none(self) -> None:
        out = _prompt()

        assert "(none)" in out

    def test_the_failure_fields_survive_bounding(self) -> None:
        """#445's whole point: the error text is what a lesson gets written
        from. Bounding must not cut it away."""
        out = _prompt(
            failures=[
                {
                    "step": "cto review",
                    "role": "cto",
                    "status": "failed",
                    "detail": "claude exited 1: Prompt is too long",
                }
            ]
        )

        assert "cto review" in out
        assert "Prompt is too long" in out

    def test_a_real_length_failure_keeps_its_diagnosis(self) -> None:
        """A short fixture would pass at any field limit and prove nothing.

        Step 393's stored `detail` is 1 973 chars — the runner caps a failure
        detail at 2 000 — and the part that names the cause is at the very
        END: `"result": "Prompt is too long"`. A 700-char excerpt would have
        thrown away exactly the sentence a lesson gets written from, and the
        short fixture above would never have noticed.
        """
        realistic = (
            'claude exited 1: {"is_error":true,"total_cost_usd":1.167931,'
            + '"usage":{"cache_read_input_tokens":71082},' * 40
            + '"result":"Prompt is too long","type":"result"}'
        )
        assert 1_500 < len(realistic) < 2_100, "fixture must match the real cap"

        out = _prompt(
            failures=[
                {"step": "cto review", "role": "cto", "status": "failed", "detail": realistic}
            ]
        )

        assert "Prompt is too long" in out, "the diagnosis is at the end and must survive"


class TestTheBoundsAreCoherent:
    def test_the_ceiling_is_below_the_os_limit(self) -> None:
        """A ceiling at or above MAX_ARG_STRLEN would not prevent the
        failure it exists to prevent."""
        assert _MAX_PROMPT_CHARS < _MAX_ARG_STRLEN

    def test_a_field_cannot_alone_exceed_the_ceiling(self) -> None:
        assert _MAX_FIELD_CHARS < _MAX_PROMPT_CHARS


class TestTheRunnerFailsLegibly:
    def test_an_oversized_prompt_names_itself(self, tmp_path) -> None:
        """`[Errno 7] Argument list too long: 'claude'` took three rounds of
        investigation to trace back to a prompt. The error should say so."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from hivepilot.config import settings
        from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
        from hivepilot.runners.base import RunnerExecutionError, RunnerPayload
        from hivepilot.runners.claude_runner import ClaudeRunner

        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude"), settings
        )
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=Path(tmp_path)),
            task_name="t",
            step=TaskStep(name="s", runner="claude"),
            metadata={"extra_prompt": "z" * (_MAX_ARG_STRLEN + 1)},
            secrets={},
        )

        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            with pytest.raises(RunnerExecutionError) as excinfo:
                runner.capture(payload)

        assert "prompt" in str(excinfo.value).lower()
        assert excinfo.value.context["prompt_bytes"] > _MAX_ARG_STRLEN
