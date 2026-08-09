"""A blocking review must leave a trace, not just block.

`_run_review`'s fail-closed early exits called `_register_verdict` — which is
purely in-memory, feeding the `perform_git_actions` PR gate — and never
`record_verdict`. So when a review blocked for one of those reasons, the gate
worked and the `verdicts` table stayed empty.

That is exactly what happened in production: the `noxys` pipeline ran its
`PR Approval` stage with `review_target: github_pr` and three reviewers
configured, `review.empty_subject` fired, promotion was blocked — and after
312 runs the operator was looking at `verdicts = 0` and concluding the
feature had never run at all.

Blocking silently is worse than not blocking: the gate is doing its job and
nothing on any surface says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.models import EffectiveDebateConfig, PipelineStage, ProjectConfig
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import db, state_service


def _effective(reviewers: list[str]) -> EffectiveDebateConfig:
    return EffectiveDebateConfig(
        enable_judge=False,
        enable_arbiter=False,
        runner="claude",
        model=None,
        confidence_threshold=0.7,
        reviewers=reviewers,
        review_target="github_pr",
    )


def _verdict_rows() -> list[dict]:
    state_service.init_db()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM verdicts ORDER BY id")]


@pytest.fixture
def orchestrator() -> Orchestrator:
    return Orchestrator()


@pytest.fixture
def stage() -> PipelineStage:
    return PipelineStage(name="PR Approval", task="noxys-cos-pr-approval")


def test_an_empty_subject_persists_its_blocking_verdict(
    orchestrator: Orchestrator, stage: PipelineStage, tmp_path: Path
) -> None:
    """The production case, exactly.

    A `PR Approval` stage reviews the working-tree diff, which is empty by
    then because earlier stages already committed. The review fails closed —
    correctly — but left no row, so `verdicts` read as "never ran".
    """
    orchestrator._run_review(
        stage=stage,
        pipeline=None,
        effective=_effective(["reviewer", "ciso", "qa"]),
        project=ProjectConfig(path=tmp_path),
        policy=None,
        subject="",
        simulate=False,
        run_id=None,
    )

    rows = _verdict_rows()
    assert len(rows) == 1, "a blocking review must be recorded, not only registered"
    assert rows[0]["decision"] is None, "no reviewer approved anything"
    assert rows[0]["kind"] == "review"


def test_the_persisted_verdict_says_why_it_blocked(
    orchestrator: Orchestrator, stage: PipelineStage, tmp_path: Path
) -> None:
    """A row that only says "blocked" sends the operator back to the logs.

    The two fail-closed exits block for different, fixable reasons — nothing
    to review, versus no reviewers configured — and the summary has to tell
    them apart.
    """
    orchestrator._run_review(
        stage=stage,
        pipeline=None,
        effective=_effective(["reviewer"]),
        project=ProjectConfig(path=tmp_path),
        policy=None,
        subject="   \n  ",
        simulate=False,
        run_id=None,
    )

    summary = (_verdict_rows()[0]["summary"] or "").lower()
    assert "no diff" in summary or "empty" in summary, f"unhelpful summary: {summary!r}"


def test_a_review_with_no_reviewers_persists_its_own_reason(
    orchestrator: Orchestrator, stage: PipelineStage, tmp_path: Path
) -> None:
    orchestrator._run_review(
        stage=stage,
        pipeline=None,
        effective=_effective([]),
        project=ProjectConfig(path=tmp_path),
        policy=None,
        subject="diff --git a/x b/x",
        simulate=False,
        run_id=None,
    )

    rows = _verdict_rows()
    assert len(rows) == 1
    summary = (rows[0]["summary"] or "").lower()
    assert "reviewer" in summary, f"must name the missing piece: {summary!r}"


def test_an_unexpected_failure_records_its_type_and_re_raises(
    orchestrator: Orchestrator,
    stage: PipelineStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path least likely to be reproduced on demand needs a row most.

    Only the exception's TYPE is persisted — its message can carry step
    output, which is redacted elsewhere for exactly that reason.
    """
    import hivepilot.orchestrator as orch

    # `**_kw` so this double keeps mirroring the real signature as it grows.
    # It gained `subject_path` when the full diff started being written to a
    # file for reviewers to `Read`; a double pinned to the old arity fails
    # with a TypeError that looks nothing like the RuntimeError under test.
    def boom(_subject: str, **_kw: object) -> str:
        raise RuntimeError("secret-bearing detail")

    monkeypatch.setattr(orch, "_build_review_challenge_prompt", boom)

    with pytest.raises(RuntimeError):
        orchestrator._run_review(
            stage=stage,
            pipeline=None,
            effective=_effective(["reviewer"]),
            project=ProjectConfig(path=tmp_path),
            policy=None,
            subject="diff --git a/x b/x",
            simulate=False,
            run_id=None,
        )

    summary = _verdict_rows()[0]["summary"] or ""
    assert "RuntimeError" in summary
    assert "secret-bearing" not in summary, "the exception message must not be persisted"


class TestReviewerDispatchIsRecorded:
    """A reviewer's work must appear in the figures like any other step.

    Reviewer dispatches went through `capture_definition` directly, never
    `record_step`. After 312 production runs there were **zero** steps named
    `review:*`, so a review round showed up in no cost total, no per-agent
    figure and no panel — and "do all three reviewers need to be on opus?"
    could not be answered, because nobody had ever measured one.
    """

    @staticmethod
    def _run(orchestrator: Orchestrator, tmp_path: Path, *, capture) -> int:
        run_id = state_service.record_run_start("noxys", "review-probe")
        orchestrator.registry.capture_definition = capture  # type: ignore[method-assign]
        try:
            orchestrator._run_review(
                stage=PipelineStage(name="Implementation", task="noxys-developer"),
                pipeline=None,
                effective=_effective(["reviewer"]),
                project=ProjectConfig(path=tmp_path),
                policy=None,
                subject="diff --git a/x b/x\n+changed",
                simulate=False,
                run_id=run_id,
            )
        except Exception:  # noqa: BLE001 — the failure path is under test too
            pass
        return run_id

    @staticmethod
    def _review_steps(run_id: int) -> list[dict]:
        return [
            dict(r)
            for r in state_service.get_steps_for_run(run_id)
            if str(r["step"]).startswith("review:")
        ]

    def test_a_successful_reviewer_is_recorded_under_its_own_role(
        self, orchestrator: Orchestrator, tmp_path: Path
    ) -> None:
        run_id = self._run(orchestrator, tmp_path, capture=lambda _d, _p: "PASS")

        steps = self._review_steps(run_id)
        assert len(steps) == 1
        assert steps[0]["step"] == "review:reviewer"
        assert steps[0]["status"] == "success"
        # The reviewing agent did this work, not the stage's task — otherwise
        # the cost lands on whoever happened to be reviewed.
        assert steps[0]["role"] == "reviewer"

    def test_a_failed_reviewer_is_recorded_too(
        self, orchestrator: Orchestrator, tmp_path: Path
    ) -> None:
        """A dispatch that raised still consumed a call and still blocked.

        Recording only successes would understate spend and hide the fact
        that the gate closed because a reviewer was unreachable.
        """

        def boom(_definition, _payload):
            raise RuntimeError("runner exploded")

        run_id = self._run(orchestrator, tmp_path, capture=boom)

        steps = self._review_steps(run_id)
        assert len(steps) == 1
        assert steps[0]["status"] == "failed"
        assert steps[0]["role"] == "reviewer"

    def test_a_simulated_review_records_nothing(
        self, orchestrator: Orchestrator, tmp_path: Path
    ) -> None:
        """A dry run costs nothing, so it must not add spend to the figures."""
        run_id = state_service.record_run_start("noxys", "review-probe")
        orchestrator.registry.capture_definition = lambda definition, payload: "PASS"  # type: ignore[method-assign,assignment]
        orchestrator._run_review(
            stage=PipelineStage(name="Implementation", task="noxys-developer"),
            pipeline=None,
            effective=_effective(["reviewer"]),
            project=ProjectConfig(path=tmp_path),
            policy=None,
            subject="diff --git a/x b/x\n+changed",
            simulate=True,
            run_id=run_id,
        )

        assert self._review_steps(run_id) == []


def test_blocking_still_blocks(
    orchestrator: Orchestrator, stage: PipelineStage, tmp_path: Path
) -> None:
    """Persistence is additive — the gate must be untouched.

    The in-memory governing verdict is what `perform_git_actions` reads;
    recording a row must not weaken it.
    """
    from hivepilot.services.git_service import is_blocking

    orchestrator._run_review(
        stage=stage,
        pipeline=None,
        effective=_effective(["reviewer"]),
        project=ProjectConfig(path=tmp_path),
        policy=None,
        subject="",
        simulate=False,
        run_id=None,
    )

    assert is_blocking(orchestrator._governing_verdict, 0.7)
