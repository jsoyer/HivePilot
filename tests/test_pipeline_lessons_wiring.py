"""A pipeline run must feed the lessons loop, not just a bare task run.

The loop is built and works: `distill_lessons` turns a run's verdicts,
interactions and outcomes into candidates, `record_lesson` persists them,
`build_lessons_context` injects the validated ones back into later prompts.
Every piece has tests.

It has produced nothing. Measured on the reference deployment:

    interactions  136
    verdicts       14
    lessons         0

The entry condition (verdicts OR interactions) has been satisfied for
weeks, and `HIVEPILOT_ENABLE_LESSON_DISTILLATION=true` is set in three
environment files. The reason is simpler and worse than a misconfiguration:
`_distill_and_persist_lessons` has exactly ONE call site, and it is in
`_run_task_body` — the bare `hivepilot run` path. `_run_pipeline_body` never
calls it.

The deployment works in pipelines. So the learning loop was wired to the
path nobody uses, and there was no signal of that anywhere: a run completes,
lessons stays 0, and nothing claims otherwise. A feature that is present,
enabled, fed, and silently unreachable.
"""

from __future__ import annotations

import pytest


class TestThePipelinePathDistills:
    def test_run_pipeline_body_reaches_the_distiller(self) -> None:
        """Asserted against the source rather than a mock run: the defect is
        a missing CALL, and only the call sites can show it. A behavioural
        test that stubbed the orchestrator would have passed against the
        broken code too — the distiller works, it was simply never invoked.
        """
        import inspect

        from hivepilot.orchestrator import Orchestrator

        body = inspect.getsource(Orchestrator._run_pipeline_body)

        assert "_distill_and_persist_lessons" in body, (
            "a pipeline run must feed the lessons loop; with this call absent "
            "the deployment accumulated 136 interactions and 14 verdicts for "
            "0 lessons"
        )

    def test_the_bare_task_path_still_distills(self) -> None:
        """The existing call site must survive: `hivepilot run` learning was
        never the broken half."""
        import inspect

        from hivepilot.orchestrator import Orchestrator

        assert "_distill_and_persist_lessons" in inspect.getsource(Orchestrator._run_task_body)


class TestItRespectsTheSameGates:
    @pytest.mark.parametrize("gate", ["simulate", "dry_run"])
    def test_the_pipeline_call_is_gated_like_the_task_call(self, gate: str) -> None:
        """Distillation makes a REAL, costed LLM call. A dry run or a
        simulation must not spend money, exactly as on the task path."""
        import inspect

        from hivepilot.orchestrator import Orchestrator

        body = inspect.getsource(Orchestrator._run_pipeline_body)
        start = body.index("_distill_and_persist_lessons")
        window = body[max(0, start - 1200) : start]

        assert gate in window, f"the pipeline distillation call must be gated on {gate}"

    def test_it_is_wrapped_so_a_failure_cannot_kill_the_run(self) -> None:
        """A broken distiller must never fail a pipeline that otherwise
        succeeded — the same contract the task path already honours."""
        import inspect

        from hivepilot.orchestrator import Orchestrator

        body = inspect.getsource(Orchestrator._run_pipeline_body)
        start = body.index("_distill_and_persist_lessons")
        window = body[max(0, start - 1200) : start + 1200]

        assert "lessons.distill_error" in window
