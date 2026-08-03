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

    def boom(_subject: str) -> str:
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
