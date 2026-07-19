"""Integration tests for the S3 adversarial-review merge/progression gate.

Sprint 3 of the "adversarial review (thin layer over debate)" feature closes
a fail-open gap: `_execute_task_body` (hivepilot/orchestrator.py) computed
`judge_gate_enabled` for `perform_git_actions` purely from
`enable_judge`/`enable_arbiter`, completely independent of the
`review_target` flag that actually drives `_run_review`/`self._governing_verdict`.
With `review_target="github_pr"` set but judge and arbiter both off, a
blocking adversarial-review verdict was silently ignored and `promote_pr`/
`merge_pr` proceeded anyway.

These tests drive the real call site inside `Orchestrator._execute_task_body`
(the same pattern `tests/test_debate_pipeline_integration.py` uses) rather
than unit-testing `perform_git_actions` in isolation, so the fix under test
-- the `judge_gate_enabled=` boolean expression itself, plus the new
`review_target="internal"` progression-halting `raise` -- is exercised for
real. Only the reviewer/judge LLM boundary is stubbed (via a preset
`orch._governing_verdict` + a mocked `orch._run_review`, mirroring
`test_debate_pipeline_integration.py`'s `TestGateBlocksOnMaintainOrLowConfidence`
pattern of presetting the verdict directly) and the `gh`-CLI boundary
(`git_service.promote_pr`/`merge_pr`) -- every resolver, gate, and the new
`ReviewBlockedError` raise all run for real.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import (
    DebateConfig,
    GitActions,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    TaskConfig,
    TasksFile,
    TaskStep,
)
from hivepilot.orchestrator import ReviewBlockedError, Verdict
from hivepilot.services.pipeline_service import validate_pipeline


def _bare_orchestrator():
    """Mirrors tests/test_debate_pipeline_integration.py's `_bare_orchestrator`
    -- a real (empty) RunnerRegistry, for `_execute_task_body`-level wiring
    tests that need a real stub runner dispatched by kind (not by role)."""
    from hivepilot.orchestrator import Orchestrator
    from hivepilot.registry import RunnerRegistry

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=MagicMock(pipelines={})),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=RunnerRegistry({})),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()
    orch.plugins = MagicMock()
    return orch


def _init_repo(tmp_path: Path) -> ProjectConfig:
    """Mirrors test_debate_pipeline_integration.py's `_init_repo`."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    return ProjectConfig(path=tmp_path)


def _register_stub_runner(kind: str) -> None:
    """Mirrors test_debate_pipeline_integration.py's `_register_stub_runner`
    -- a minimal capture-only runner registered under *kind* in the real,
    module-global RUNNER_MAP so `_execute_task_body` can dispatch a real
    (non-role) step without touching any actual agent binary."""
    from hivepilot.registry import RunnerRegistry
    from hivepilot.runners.base import BaseRunner, RunnerPayload

    class _StubRunner(BaseRunner):
        supported_modes = frozenset({"cli"})

        def __init__(self, definition, settings) -> None:  # noqa: ANN001
            self.definition = definition
            self.settings = settings

        def run(self, payload: RunnerPayload) -> None:  # pragma: no cover - unused
            pass

        def capture(self, payload: RunnerPayload) -> str:
            return "ok"

    RunnerRegistry.register(kind, _StubRunner, override=True)


class TestGithubPrTargetGatesMergeOnReviewVerdict:
    """The exact S3 fail-open fix: `review_target="github_pr"` must gate
    `promote_pr`/`merge_pr` on its own, independent of `enable_judge`/
    `enable_arbiter`."""

    def test_blocking_review_verdict_skips_promote_and_merge_judge_and_arbiter_off(
        self, tmp_path: Path
    ) -> None:
        """CORRECTNESS (the S3 fix): review_target=github_pr + a blocking
        governing verdict (as a REJECT/BLOCKED reviewer output would
        register) + judge AND arbiter both disabled must still skip
        promote_pr/merge_pr. This assertion FAILS on pre-S3 code, where
        `judge_gate_enabled` only looks at enable_judge/enable_arbiter and
        ignores review_target entirely -- the governing verdict is silently
        never consulted and the merge proceeds (fail-open)."""
        kind = "s3-github-pr-reject-stub"
        _register_stub_runner(kind)
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner=kind)],
                git=GitActions(promote_pr=True, merge_pr=True),
            )
            project = _init_repo(tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(reviewers=["reviewer"], review_target="github_pr"),
            )
            stage = PipelineStage(name="s", task="t")

            # Simulate a reviewer that rejected the diff: `_run_review` would
            # register exactly this verdict via `_register_verdict`. Mocking
            # `_run_review` itself (rather than driving a real role-based
            # reviewer dispatch) isolates this test to the gate-wiring bug
            # S3 fixes -- the reviewer's own dispatch/parsing is already
            # covered by tests/test_orchestrator_review_path.py (S2).
            orch._governing_verdict = Verdict(decision=None, confidence=None)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch.object(orch, "_run_review") as mock_review,
                patch("hivepilot.services.git_service.promote_pr") as mock_promote,
                patch("hivepilot.services.git_service.merge_pr") as mock_merge,
            ):
                orch._execute_task_body(
                    project=project,
                    task_name="t",
                    task=task,
                    extra_prompt=None,
                    auto_git=True,
                    run_id=None,
                    policy=None,
                    simulate=False,
                    dry_run=True,
                    stage=stage,
                    pipeline=pipeline,
                )

            mock_review.assert_called_once()
            mock_promote.assert_not_called()
            mock_merge.assert_not_called()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop(kind, None)

    def test_approving_review_verdict_lets_promote_proceed_judge_and_arbiter_off(
        self, tmp_path: Path
    ) -> None:
        """Control case: the same wiring must NOT over-block -- an approving
        (ACCEPT, high-confidence) governing verdict with review_target=github_pr
        and judge/arbiter off must let promote_pr proceed."""
        kind = "s3-github-pr-accept-stub"
        _register_stub_runner(kind)
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner=kind)],
                git=GitActions(promote_pr=True),
            )
            project = _init_repo(tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(reviewers=["reviewer"], review_target="github_pr"),
            )
            stage = PipelineStage(name="s", task="t")

            orch._governing_verdict = Verdict(decision="ACCEPT", confidence=1.0)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch.object(orch, "_run_review") as mock_review,
                patch("hivepilot.services.git_service.promote_pr") as mock_promote,
            ):
                orch._execute_task_body(
                    project=project,
                    task_name="t",
                    task=task,
                    extra_prompt=None,
                    auto_git=True,
                    run_id=None,
                    policy=None,
                    simulate=False,
                    dry_run=True,
                    stage=stage,
                    pipeline=pipeline,
                )

            mock_review.assert_called_once()
            mock_promote.assert_called_once()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop(kind, None)


class TestReviewTargetUnsetIsByteIdentical:
    """Regression: no `debate:`/`review_target` at all must behave exactly
    like pre-Sprint-1 HivePilot -- `_run_review` never runs, and a stray/
    leftover `_governing_verdict` from something unrelated must never gate
    promote_pr (only `enable_judge`/`enable_arbiter` may do that, and both
    are off here)."""

    def test_promote_proceeds_ignoring_stray_governing_verdict(self, tmp_path: Path) -> None:
        kind = "s3-unset-stub"
        _register_stub_runner(kind)
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner=kind)],
                git=GitActions(promote_pr=True),
            )
            project = _init_repo(tmp_path)
            pipeline = PipelineConfig(description="d", stages=[], debate=None)
            stage = PipelineStage(name="s", task="t")

            # A stray blocking verdict left over from something unrelated --
            # with review_target unset AND judge/arbiter off, this must be
            # completely ignored (judge_gate_enabled stays False).
            orch._governing_verdict = Verdict(decision=None, confidence=None)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch.object(orch, "_run_review") as mock_review,
                patch("hivepilot.services.git_service.promote_pr") as mock_promote,
            ):
                orch._execute_task_body(
                    project=project,
                    task_name="t",
                    task=task,
                    extra_prompt=None,
                    auto_git=True,
                    run_id=None,
                    policy=None,
                    simulate=False,
                    dry_run=True,
                    stage=stage,
                    pipeline=pipeline,
                )

            mock_review.assert_not_called()
            mock_promote.assert_called_once()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop(kind, None)


class TestInternalTargetGatesStageProgression:
    """`review_target="internal"` has no PR to gate -- a blocking verdict
    must instead halt stage/pipeline progression by raising, and must never
    reach `perform_git_actions` at all."""

    def test_blocking_review_verdict_raises_and_never_reaches_git_actions(
        self, tmp_path: Path
    ) -> None:
        kind = "s3-internal-reject-stub"
        _register_stub_runner(kind)
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner=kind)],
                git=GitActions(promote_pr=True, merge_pr=True),
            )
            project = _init_repo(tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(reviewers=["reviewer"], review_target="internal"),
            )
            stage = PipelineStage(name="s", task="t")

            orch._governing_verdict = Verdict(decision=None, confidence=None)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch.object(orch, "_run_review") as mock_review,
                patch("hivepilot.orchestrator.perform_git_actions") as mock_perform_git,
                pytest.raises(ReviewBlockedError, match="review_target=internal"),
            ):
                orch._execute_task_body(
                    project=project,
                    task_name="t",
                    task=task,
                    extra_prompt=None,
                    auto_git=True,
                    run_id=None,
                    policy=None,
                    simulate=False,
                    dry_run=True,
                    stage=stage,
                    pipeline=pipeline,
                )

            mock_review.assert_called_once()
            mock_perform_git.assert_not_called()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop(kind, None)

    def test_approving_review_verdict_does_not_raise(self, tmp_path: Path) -> None:
        """Control case: internal target must not over-block -- an approving
        verdict must let the stage complete normally (no raise)."""
        kind = "s3-internal-accept-stub"
        _register_stub_runner(kind)
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner=kind)],
            )
            project = _init_repo(tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(reviewers=["reviewer"], review_target="internal"),
            )
            stage = PipelineStage(name="s", task="t")

            orch._governing_verdict = Verdict(decision="ACCEPT", confidence=1.0)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch.object(orch, "_run_review") as mock_review,
            ):
                result = orch._execute_task_body(
                    project=project,
                    task_name="t",
                    task=task,
                    extra_prompt=None,
                    auto_git=False,
                    run_id=None,
                    policy=None,
                    simulate=False,
                    dry_run=True,
                    stage=stage,
                    pipeline=pipeline,
                )

            mock_review.assert_called_once()
            assert result == "ok"
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop(kind, None)


class TestValidatePipelineReviewCrossBlockLoadTime:
    """`validate_pipeline` must catch the SAME fail-closed cross-block rule
    `resolve_debate_config` enforces mid-run -- at pipeline LOAD time
    instead, before any stage executes."""

    def test_pipeline_level_review_target_without_reviewers_raises_at_load(self) -> None:
        # Independently valid at load (DebateConfig's own model validator
        # only blocks an EXPLICIT empty `reviewers: []` in the same block;
        # `reviewers` left unset/None here is not that case) -- but resolves
        # to zero reviewers anywhere in the chain, which the cross-block
        # backstop must catch, surfaced by validate_pipeline instead of only
        # being caught when the stage actually executes.
        pipeline = PipelineConfig(
            description="d",
            debate=DebateConfig(review_target="github_pr"),
            stages=[PipelineStage(name="s", task="t")],
        )
        tasks = TasksFile(tasks={"t": TaskConfig(description="d")})
        with pytest.raises(ValueError, match="reviewers"):
            validate_pipeline(pipeline, tasks)

    def test_stage_level_review_target_without_reviewers_raises_at_load(self) -> None:
        pipeline = PipelineConfig(
            description="d",
            stages=[
                PipelineStage(
                    name="risky-stage", task="t", debate=DebateConfig(review_target="internal")
                )
            ],
        )
        tasks = TasksFile(tasks={"t": TaskConfig(description="d")})
        with pytest.raises(ValueError, match="reviewers"):
            validate_pipeline(pipeline, tasks)

    def test_review_target_with_reviewers_set_does_not_raise(self) -> None:
        pipeline = PipelineConfig(
            description="d",
            debate=DebateConfig(reviewers=["reviewer"], review_target="github_pr"),
            stages=[PipelineStage(name="s", task="t")],
        )
        tasks = TasksFile(tasks={"t": TaskConfig(description="d")})
        validate_pipeline(pipeline, tasks)  # must not raise

    def test_no_debate_block_does_not_raise(self) -> None:
        pipeline = PipelineConfig(description="d", stages=[PipelineStage(name="s", task="t")])
        tasks = TasksFile(tasks={"t": TaskConfig(description="d")})
        validate_pipeline(pipeline, tasks)  # must not raise
