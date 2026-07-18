"""Orchestrator-level tests for the sticky "governing verdict" aggregate
(Debate Judge & Consensus PRD, Sprint 3) — the exact mechanism that keeps the
fail-closed `perform_git_actions` PR gate fail-closed once a debate-judge or
challenge-arbiter Verdict has been produced during a run.

Covers (adversarial-review MED test-gap):
- `Orchestrator._register_verdict` is STICKY: once a blocking Verdict is
  registered, a LATER approving Verdict from an unrelated stage/challenge
  must never overwrite it (block-then-approve stays blocked).
- The reverse (approve-then-block) DOES update the governing verdict to the
  blocking one.
- `Orchestrator._enter_run_scope` resets `_governing_verdict` to `None` on
  entering a fresh OUTERMOST run scope, so a stale verdict from a prior
  run_task/run_pipeline/run_debate call can never leak into the next run's
  gate decision.
- `Orchestrator._resolve_challenge_via_arbiter`'s exception path registers an
  explicit BLOCKING failure Verdict (decision=None, confidence=None) — a
  broken arbiter call can never leave a prior approval governing — while
  still reaching human escalation (`stream_needs_human`).
- With judge gating enabled but `_register_verdict` never called (no
  challenge/debate ever ran), `_governing_verdict` stays `None`, and feeding
  that `None` into `perform_git_actions` blocks promote_pr (fail-closed).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import (
    GitActions,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    TaskConfig,
    TaskStep,
)
from hivepilot.orchestrator import Verdict
from hivepilot.services import git_service
from hivepilot.services import notification_service as ns
from hivepilot.services.git_service import is_blocking

# ---------------------------------------------------------------------------
# Helpers — mirrors tests/test_challenge_arbiter.py / test_challenge_rebuttal.py
# ---------------------------------------------------------------------------


def _make_pipeline(*stage_defs: tuple[str, str]) -> PipelineConfig:
    stages = [PipelineStage(name=name, task=task) for name, task in stage_defs]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator():
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipeline = _make_pipeline(("planning", "plan-task"), ("review", "review-task"))
    pipelines_file = PipelinesFile(pipelines={"test-pipe": pipeline})

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        orch = Orchestrator()

    return orch


def _wire_stages(orch) -> tuple[PipelineStage, PipelineStage]:
    plan_task = TaskConfig(
        description="plan",
        role="ceo",
        engine="native",
        steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
    )
    review_task = TaskConfig(
        description="review",
        role="reviewer",
        engine="native",
        steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
    )
    orch.tasks = MagicMock()
    orch.tasks.tasks = {"plan-task": plan_task, "review-task": review_task}

    project = ProjectConfig(path=Path("/tmp/test-project"))
    orch.projects = MagicMock()
    orch.projects.projects = {"test-project": project}

    upstream = PipelineStage(name="planning", task="plan-task")
    challenger_stage = PipelineStage(name="review", task="review-task")
    return upstream, challenger_stage


def _role_patches():
    """Common patch set mirroring test_challenge_rebuttal.py's role resolution."""
    return (
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-5", None)),
        patch("hivepilot.roles.resolve_host", return_value=None),
        patch(
            "hivepilot.roles.get_role",
            side_effect=lambda k: MagicMock(
                permission_mode=None,
                prompt_file=Path("/tmp/nonexistent.md"),
                display_name="Aliénor" if k == "ceo" else "Victor",
                title="CEO" if k == "ceo" else "Reviewer",
            ),
        ),
        patch("hivepilot.services.interaction_service.log_challenge_interaction"),
    )


def _run_rebuttal(orch, upstream, challenger_stage, prior_chunks):
    p1, p2, p3, p4 = _role_patches()
    with p1, p2, p3, p4:
        orch._run_rebuttal_round(
            challenger_name="Victor (Reviewer)",
            challenge_target="Aliénor (CEO)",
            challenge_point="The roadmap is unrealistic.",
            challenger_stage=challenger_stage,
            completed_stages=[upstream],
            prior_chunks=prior_chunks,
            policy=None,
            project_name="test-project",
            simulate=False,
        )


@pytest.fixture(autouse=True)
def _reset_arbiter_flag() -> Iterator[None]:
    """Guarantee the opt-in flag/threshold never leak between tests."""
    original_flag = settings.enable_challenge_arbiter
    original_threshold = settings.judge_confidence_threshold
    yield
    settings.enable_challenge_arbiter = original_flag
    settings.judge_confidence_threshold = original_threshold


@pytest.fixture(autouse=True)
def _mock_streams(monkeypatch: pytest.MonkeyPatch):
    resolved_calls: list[tuple] = []
    needs_human_calls: list[tuple] = []
    monkeypatch.setattr(ns, "stream_rebuttal", lambda **kw: None)
    monkeypatch.setattr(
        ns,
        "stream_resolved",
        lambda actor, target, resolution: resolved_calls.append((actor, target, resolution)),
    )
    monkeypatch.setattr(
        ns,
        "stream_needs_human",
        lambda actor, target, point: needs_human_calls.append((actor, target, point)),
    )
    return resolved_calls, needs_human_calls


# ---------------------------------------------------------------------------
# _register_verdict — sticky most-blocking-wins aggregation
# ---------------------------------------------------------------------------


class TestRegisterVerdictSticky:
    def test_block_then_approve_stays_blocked(self) -> None:
        """A blocking verdict registered first must NOT be overwritten by a
        later approving one — approve can never erase a prior block."""
        orch = _make_orchestrator()
        blocking = Verdict(decision="MAINTAIN", confidence=0.9)
        approving = Verdict(decision="ACCEPT", confidence=0.9)

        orch._register_verdict(blocking)
        orch._register_verdict(approving)

        assert orch._governing_verdict is blocking
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is True, (
            "governing verdict must still be blocking after a later approval was registered"
        )

    def test_block_then_approve_gate_still_blocks(self, tmp_path: Path) -> None:
        """Behavioral check: feeding the (still-blocking) governing verdict
        into perform_git_actions must skip promote_pr."""
        orch = _make_orchestrator()
        orch._register_verdict(Verdict(decision="DEFEND", confidence=0.9))
        orch._register_verdict(Verdict(decision="ACCEPT", confidence=0.9))

        import git as gitlib

        gitlib.Repo.init(tmp_path)
        project = ProjectConfig(path=tmp_path)
        ga = GitActions(promote_pr=True)
        with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
            git_service.perform_git_actions(
                project_name="p",
                project=project,
                git=ga,
                verdict=orch._governing_verdict,
                judge_gate_enabled=True,
                confidence_threshold=settings.judge_confidence_threshold,
            )
        mock_promote.assert_not_called()

    def test_approve_then_block_governing_becomes_block(self) -> None:
        """The reverse order: an approving verdict registered first IS
        overwritten once a later blocking verdict is registered."""
        orch = _make_orchestrator()
        approving = Verdict(decision="ACCEPT", confidence=0.9)
        blocking = Verdict(decision="MAINTAIN", confidence=0.9)

        orch._register_verdict(approving)
        assert orch._governing_verdict is approving
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is False

        orch._register_verdict(blocking)
        assert orch._governing_verdict is blocking
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is True

    def test_approve_then_block_gate_blocks(self, tmp_path: Path) -> None:
        orch = _make_orchestrator()
        orch._register_verdict(Verdict(decision="ACCEPT", confidence=0.9))
        orch._register_verdict(Verdict(decision="MAINTAIN", confidence=0.9))

        import git as gitlib

        gitlib.Repo.init(tmp_path)
        project = ProjectConfig(path=tmp_path)
        ga = GitActions(promote_pr=True)
        with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
            git_service.perform_git_actions(
                project_name="p",
                project=project,
                git=ga,
                verdict=orch._governing_verdict,
                judge_gate_enabled=True,
                confidence_threshold=settings.judge_confidence_threshold,
            )
        mock_promote.assert_not_called()

    def test_none_verdict_is_a_noop(self) -> None:
        """Registering None must not clobber an existing governing verdict —
        `is_blocking(None, ...)` already fails closed on its own, so
        `_register_verdict` treats it as nothing-to-register."""
        orch = _make_orchestrator()
        approving = Verdict(decision="ACCEPT", confidence=0.9)
        orch._register_verdict(approving)
        orch._register_verdict(None)
        assert orch._governing_verdict is approving


# ---------------------------------------------------------------------------
# _enter_run_scope — cross-run reset
# ---------------------------------------------------------------------------


class TestGoverningVerdictResetsAcrossRuns:
    def test_fresh_orchestrator_governing_verdict_is_none(self) -> None:
        orch = _make_orchestrator()
        assert orch._governing_verdict is None

    def test_entering_new_outermost_scope_resets_stale_block(self) -> None:
        """A blocking verdict left over from some prior activity must be
        cleared the moment a NEW outermost run scope begins — otherwise it
        would wrongly govern the next, unrelated run's gate decision."""
        orch = _make_orchestrator()
        orch._register_verdict(Verdict(decision="MAINTAIN", confidence=0.9))
        assert orch._governing_verdict is not None

        orch._enter_run_scope()  # depth 0 -> 1: a fresh outermost run begins
        assert orch._governing_verdict is None
        orch._exit_run_scope()

    def test_entering_new_outermost_scope_resets_stale_approval_too(self) -> None:
        """Same reset applies to a stale APPROVING verdict — a prior run's
        approval must not silently govern a new run that never itself
        produced a verdict (that new run must fail closed on its own)."""
        orch = _make_orchestrator()
        orch._register_verdict(Verdict(decision="ACCEPT", confidence=0.9))
        assert orch._governing_verdict is not None

        orch._enter_run_scope()
        assert orch._governing_verdict is None
        orch._exit_run_scope()

    def test_nested_scope_entry_does_not_reset(self) -> None:
        """Only the OUTERMOST scope entry resets — a nested run_task call
        inside an already-running run_pipeline (depth 1 -> 2) must not wipe
        out a verdict registered earlier in the same run."""
        orch = _make_orchestrator()
        orch._enter_run_scope()  # depth 0 -> 1 (outermost)
        blocking = Verdict(decision="MAINTAIN", confidence=0.9)
        orch._register_verdict(blocking)

        orch._enter_run_scope()  # depth 1 -> 2 (nested — must NOT reset)
        assert orch._governing_verdict is blocking

        orch._exit_run_scope()  # back to depth 1
        assert orch._governing_verdict is blocking

        orch._exit_run_scope()  # back to depth 0


# ---------------------------------------------------------------------------
# _resolve_challenge_via_arbiter exception path -> registers a BLOCKING
# failure Verdict, and human escalation is still reached
# ---------------------------------------------------------------------------


class TestArbiterExceptionRegistersBlockingVerdict:
    def test_exception_registers_blocking_failure_verdict(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        # A prior stage already registered an APPROVING verdict — the broken
        # arbiter call below must NOT leave that stale approval governing.
        orch._register_verdict(Verdict(decision="ACCEPT", confidence=0.9))

        orch.registry.capture_definition.return_value = "DEFEND: My analysis is correct."

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with patch.object(
            orch, "_adjudicate_challenge", side_effect=RuntimeError("judge runner crashed")
        ):
            # Must not raise — the pipeline continues with an escalation.
            _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        assert orch._governing_verdict is not None
        assert orch._governing_verdict.decision is None
        assert orch._governing_verdict.confidence is None
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is True, (
            "a broken arbiter call must register as BLOCKING, never leave a prior approval standing"
        )

        resolved_calls, needs_human_calls = _mock_streams
        assert len(needs_human_calls) == 1, "human escalation must still be reached"
        assert len(resolved_calls) == 0
        assert any("[NEEDS_HUMAN]" in c for c in prior_chunks)

    def test_exception_failure_verdict_blocks_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams, tmp_path: Path
    ) -> None:
        """Behavioral check: after the arbiter-exception path runs, feeding
        the resulting governing verdict into perform_git_actions blocks
        promote_pr."""
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "DEFEND: My analysis is correct."

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with patch.object(
            orch, "_adjudicate_challenge", side_effect=RuntimeError("judge runner crashed")
        ):
            _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        import git as gitlib

        gitlib.Repo.init(tmp_path)
        project = ProjectConfig(path=tmp_path)
        ga = GitActions(promote_pr=True)
        with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
            git_service.perform_git_actions(
                project_name="p",
                project=project,
                git=ga,
                verdict=orch._governing_verdict,
                judge_gate_enabled=True,
                confidence_threshold=settings.judge_confidence_threshold,
            )
        mock_promote.assert_not_called()


# ---------------------------------------------------------------------------
# Never-registered governing verdict -> fail-closed at the gate
# ---------------------------------------------------------------------------


class TestNeverRegisteredFailsClosed:
    def test_governing_verdict_none_when_never_registered(self) -> None:
        orch = _make_orchestrator()
        assert orch._governing_verdict is None
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is True

    def test_gate_blocks_when_no_verdict_ever_registered(self, tmp_path: Path) -> None:
        """With judge gating enabled but no debate/challenge ever run this
        run, `_governing_verdict` stays None -- the gate must still fail
        closed and skip promote_pr."""
        orch = _make_orchestrator()

        import git as gitlib

        gitlib.Repo.init(tmp_path)
        project = ProjectConfig(path=tmp_path)
        ga = GitActions(promote_pr=True, merge_pr=True)
        with (
            patch("hivepilot.services.git_service.promote_pr") as mock_promote,
            patch("hivepilot.services.git_service.merge_pr") as mock_merge,
        ):
            git_service.perform_git_actions(
                project_name="p",
                project=project,
                git=ga,
                verdict=orch._governing_verdict,
                judge_gate_enabled=True,
                confidence_threshold=settings.judge_confidence_threshold,
            )
        mock_promote.assert_not_called()
        mock_merge.assert_not_called()
