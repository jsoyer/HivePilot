"""Tests for the adversarial-review orchestrator wiring
(adversarial-review fail-open fix, Sprint 2 follow-up) — `Orchestrator._run_review`,
the method that turns the configured `EffectiveDebateConfig.reviewers` /
`.review_target` (resolved by Sprint 1's `resolve_debate_config`) into an
adversarial-challenge round over a stage's produced diff.

SECURITY FIX covered here: the original Sprint 2 implementation synthesized
every reviewer's challenge through the debate-judge arbiter
(`Orchestrator._adjudicate` / `_parse_verdict`) and registered THE JUDGE'S
verdict — fail-open, because a confident-but-wrong judge decision could
overturn every reviewer's rejection. The judge is NEVER consulted for the
review path anymore: each reviewer's own explicit `status:` token (PASS |
REQUEST_CHANGES | BLOCKED | NEEDS_HUMAN, per `prompts/agents/reviewer.md`) is
parsed directly and aggregated with deterministic boolean logic — ALL
reviewers must explicitly PASS for a non-blocking verdict.

Covers:
- Deterministic verdict: a reviewer's own BLOCKED/REQUEST_CHANGES token
  governs the verdict directly (no judge/arbiter involved at all).
- All-PASS is not blocking.
- Empty/ambiguous/unparseable reviewer output blocks (never an implicit
  pass).
- Fail-closed: an unknown/unresolvable reviewer role registers an explicit
  BLOCKING verdict — never skipped, never an implicit ACCEPT — and this
  holds even when OTHER configured reviewers resolve fine and PASS.
- Fail-closed: an empty subject (no diff to show reviewers) blocks without
  ever calling a reviewer.
- Sticky: a review REJECT is never cleared by a later, unrelated ACCEPT.
- Regression: `review_target` unset never enters the review path at all —
  `Orchestrator._execute_task_body`'s wiring is a no-op for flags-off runs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import (
    DebateConfig,
    EffectiveDebateConfig,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    TaskConfig,
    TaskStep,
)
from hivepilot.orchestrator import Verdict
from hivepilot.services import config_provenance
from hivepilot.services.git_service import is_blocking

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


# ---------------------------------------------------------------------------
# Helpers — mirrors tests/test_challenge_arbiter.py / test_governing_verdict.py
# ---------------------------------------------------------------------------


def _make_orchestrator():
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={})

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        orch = Orchestrator()
    orch.registry = MagicMock()
    return orch


def _bare_orchestrator():
    """Mirrors tests/test_debate_pipeline_integration.py's `_bare_orchestrator`
    — a real (empty) RunnerRegistry, for `_execute_task_body`-level wiring
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


def _register_stub_runner(kind: str) -> None:
    """Mirrors tests/test_debate_pipeline_integration.py's
    `_register_stub_runner` — a minimal capture-only runner registered under
    *kind* in the real, module-global RUNNER_MAP so `_execute_task_body` can
    dispatch a real (non-role) step without touching any actual agent
    binary."""
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


def _known_role(name: str) -> MagicMock:
    return MagicMock(
        permission_mode=None,
        prompt_file=Path("/tmp/nonexistent.md"),
        display_name=name,
        title=name,
    )


def _effective(
    *, reviewers: list[str], review_target: str | None = "internal"
) -> EffectiveDebateConfig:
    return EffectiveDebateConfig(
        enable_judge=False,
        enable_arbiter=False,
        runner="claude",
        model=None,
        confidence_threshold=0.5,
        reviewers=reviewers,
        review_target=review_target,
    )


def _project() -> ProjectConfig:
    return ProjectConfig(path=Path("/tmp/review-test-project"))


@contextlib.contextmanager
def _patch_reviewer_roles() -> Iterator[None]:
    """Shared context manager for the three role-resolution patches every
    `_run_review` test needs (`get_role`/`resolve_runner`/`resolve_host`) --
    avoids repeating them (and avoids starred-expression unpacking, which is
    not valid inside a parenthesized `with (...)` statement)."""
    with (
        patch("hivepilot.roles.get_role", side_effect=lambda k: _known_role(k)),
        patch(
            "hivepilot.roles.resolve_runner",
            return_value=("claude", "claude-sonnet-4-5", None),
        ),
        patch("hivepilot.roles.resolve_host", return_value=None),
    ):
        yield


# ---------------------------------------------------------------------------
# Deterministic verdict: the reviewer's own token governs, never the judge
# ---------------------------------------------------------------------------


class TestDeterministicReviewerVerdict:
    def test_reviewer_output_BLOCKED_deterministically_blocks(self) -> None:
        """A reviewer's own explicit BLOCKED token must govern the verdict —
        no judge/arbiter is ever consulted; the block comes straight from
        the reviewer's own output."""
        orch = _make_orchestrator()
        orch.registry.capture_definition.return_value = (
            "status: BLOCKED\nThis change lacks test coverage and hardcodes a secret."
        )
        effective = _effective(reviewers=["reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            _patch_reviewer_roles(),
            patch("hivepilot.services.state_service.record_verdict"),
            patch.object(orch, "_adjudicate") as mock_adjudicate,
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff --git a/foo.py b/foo.py\n+bug",
                simulate=False,
                run_id=None,
            )

        mock_adjudicate.assert_not_called()
        orch.registry.capture_definition.assert_called_once()
        assert orch._governing_verdict is not None
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True

    def test_reviewer_output_REQUEST_CHANGES_deterministically_blocks(self) -> None:
        """Same as BLOCKED — REQUEST_CHANGES is one of the reviewer's four
        valid tokens and must also block, without any judge involvement."""
        orch = _make_orchestrator()
        orch.registry.capture_definition.return_value = (
            "status: REQUEST_CHANGES\nMissing error handling on the new endpoint."
        )
        effective = _effective(reviewers=["reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            _patch_reviewer_roles(),
            patch("hivepilot.services.state_service.record_verdict"),
            patch.object(orch, "_adjudicate") as mock_adjudicate,
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff --git a/foo.py b/foo.py\n+bug",
                simulate=False,
                run_id=None,
            )

        mock_adjudicate.assert_not_called()
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True

    def test_all_reviewers_PASS_is_not_blocking(self) -> None:
        """Every configured reviewer explicitly passing is the ONLY way the
        review registers as non-blocking."""
        orch = _make_orchestrator()
        orch.registry.capture_definition.side_effect = [
            "status: PASS\nNo concerns after adversarial review.",
            "status: PASS\nLooks correct and well tested.",
        ]
        effective = _effective(reviewers=["reviewer_one", "reviewer_two"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            _patch_reviewer_roles(),
            patch("hivepilot.services.state_service.record_verdict"),
            patch.object(orch, "_adjudicate") as mock_adjudicate,
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff --git a/foo.py b/foo.py\n+fix",
                simulate=False,
                run_id=None,
            )

        mock_adjudicate.assert_not_called()
        assert orch._governing_verdict == Verdict(decision="ACCEPT", confidence=1.0)
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is False

    def test_reviewer_empty_output_blocks(self) -> None:
        """A reviewer that returns nothing (or only whitespace) has produced
        no explicit PASS — must block, never default to approval."""
        orch = _make_orchestrator()
        orch.registry.capture_definition.return_value = "   "
        effective = _effective(reviewers=["reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            _patch_reviewer_roles(),
            patch("hivepilot.services.state_service.record_verdict"),
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff",
                simulate=False,
                run_id=None,
            )

        assert orch._governing_verdict == Verdict(decision=None, confidence=None)
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True

    def test_reviewer_ambiguous_output_blocks(self) -> None:
        """Output with no recognisable `status:` token at all must block —
        never an implicit pass just because nothing was explicitly
        rejected."""
        orch = _make_orchestrator()
        orch.registry.capture_definition.return_value = (
            "This looks fine to me, I have no further comments."
        )
        effective = _effective(reviewers=["reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            _patch_reviewer_roles(),
            patch("hivepilot.services.state_service.record_verdict"),
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff",
                simulate=False,
                run_id=None,
            )

        assert orch._governing_verdict == Verdict(decision=None, confidence=None)
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True

    def test_reviewer_conflicting_status_lines_blocks(self) -> None:
        """Multiple `status:` lines naming DIFFERENT tokens are ambiguous —
        never guess which one governs; block instead. (`_parse_reviewer_
        verdict` only recognises a `status:` token anchored at the start of
        its own line -- per the reviewer prompt's Required Output Format --
        so both conflicting tokens here are each on their own line, not
        buried mid-sentence.)"""
        orch = _make_orchestrator()
        orch.registry.capture_definition.return_value = (
            "Some rationale here.\nstatus: PASS\nOn reflection:\nstatus: BLOCKED"
        )
        effective = _effective(reviewers=["reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            _patch_reviewer_roles(),
            patch("hivepilot.services.state_service.record_verdict"),
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff",
                simulate=False,
                run_id=None,
            )

        assert orch._governing_verdict == Verdict(decision=None, confidence=None)
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True


# ---------------------------------------------------------------------------
# Fail-closed: unknown/unresolvable reviewer role
# ---------------------------------------------------------------------------


class TestUnknownReviewerRoleFailsClosed:
    def test_all_reviewers_unresolvable_registers_blocking_without_calling_runner(self) -> None:
        orch = _make_orchestrator()
        effective = _effective(reviewers=["ghost_reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.roles.get_role", side_effect=KeyError("ghost_reviewer")),
            patch("hivepilot.services.state_service.record_verdict"),
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff",
                simulate=False,
                run_id=None,
            )

        assert orch._governing_verdict == Verdict(decision=None, confidence=None)
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True
        # No reviewer runner call is ever attempted over zero resolvable roles.
        orch.registry.capture_definition.assert_not_called()

    def test_partial_unresolvable_reviewer_still_blocks_despite_other_pass(self) -> None:
        """Even when ONE configured reviewer resolves fine and explicitly
        PASSes, an unrelated unresolvable reviewer in the SAME configured
        list must still leave the governing verdict blocking (fail-closed,
        never silently dropped) — ALL reviewers must pass, not just the
        ones that resolved."""
        orch = _make_orchestrator()
        orch.registry.capture_definition.return_value = (
            "status: PASS\nNo concerns after adversarial review."
        )
        effective = _effective(reviewers=["good_reviewer", "ghost_reviewer"])

        def _get_role(name: str):
            if name == "ghost_reviewer":
                raise KeyError(name)
            return _known_role(name)

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.roles.get_role", side_effect=_get_role),
            patch(
                "hivepilot.roles.resolve_runner",
                return_value=("claude", "claude-sonnet-4-5", None),
            ),
            patch("hivepilot.roles.resolve_host", return_value=None),
            patch("hivepilot.services.state_service.record_verdict"),
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff",
                simulate=False,
                run_id=None,
            )

        assert orch._governing_verdict == Verdict(decision=None, confidence=None)
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True


# ---------------------------------------------------------------------------
# Fail-closed: no diff/output to show reviewers
# ---------------------------------------------------------------------------


class TestEmptySubjectFailsClosed:
    def test_empty_subject_blocks_without_calling_reviewer(self) -> None:
        """`_run_review` called with an empty/whitespace subject must block
        immediately, without resolving or calling any reviewer — you cannot
        review what you cannot see."""
        orch = _make_orchestrator()
        effective = _effective(reviewers=["reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.roles.get_role") as mock_get_role,
            patch("hivepilot.services.state_service.record_verdict"),
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="   ",
                simulate=False,
                run_id=None,
            )

        mock_get_role.assert_not_called()
        orch.registry.capture_definition.assert_not_called()
        assert orch._governing_verdict == Verdict(decision=None, confidence=None)
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True

    def test_empty_subject_blocks(self, tmp_path: Path) -> None:
        """End-to-end through `_execute_task_body`: when `_git_diff` returns
        an empty diff for a stage with `review_target` set, the review path
        still runs (not skipped) and registers a blocking verdict — an empty
        diff must never be fed to reviewers as if there were nothing to
        object to."""
        orch = _bare_orchestrator()
        _register_stub_runner("review-empty-subject-stub")
        try:
            task = TaskConfig(
                description="d", steps=[TaskStep(name="s", runner="review-empty-subject-stub")]
            )
            project = ProjectConfig(path=tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(reviewers=["reviewer"], review_target="internal"),
            )
            stage = PipelineStage(name="s", task="t")

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch("hivepilot.orchestrator.Orchestrator._git_diff", return_value=""),
                patch("hivepilot.roles.get_role") as mock_get_role,
                patch("hivepilot.services.state_service.record_verdict"),
            ):
                orch._execute_task_body(
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

            mock_get_role.assert_not_called()
            assert orch._governing_verdict == Verdict(decision=None, confidence=None)
            assert is_blocking(orch._governing_verdict, 0.5) is True
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop("review-empty-subject-stub", None)


# ---------------------------------------------------------------------------
# Sticky: a review REJECT is never cleared by a later unrelated ACCEPT
# ---------------------------------------------------------------------------


class TestStickyReviewRejectNotClearedByLaterAccept:
    def test_review_reject_then_unrelated_accept_stays_blocked(self) -> None:
        orch = _make_orchestrator()
        orch.registry.capture_definition.return_value = (
            "status: BLOCKED\nThis change must not be accepted: it breaks the public API."
        )
        effective = _effective(reviewers=["reviewer"])

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            _patch_reviewer_roles(),
            patch("hivepilot.services.state_service.record_verdict"),
        ):
            orch._run_review(
                stage=None,
                pipeline=None,
                effective=effective,
                project=_project(),
                policy=None,
                subject="diff",
                simulate=False,
                run_id=None,
            )

        blocking_verdict = orch._governing_verdict
        assert is_blocking(blocking_verdict, effective.confidence_threshold) is True

        # A later, unrelated stage/challenge produces a confident ACCEPT --
        # must never overwrite the review's REJECT.
        orch._register_verdict(
            Verdict(decision="ACCEPT", confidence=0.99),
            confidence_threshold=effective.confidence_threshold,
        )

        assert orch._governing_verdict is blocking_verdict
        assert is_blocking(orch._governing_verdict, effective.confidence_threshold) is True


# ---------------------------------------------------------------------------
# Regression: review_target unset never enters the review path
# ---------------------------------------------------------------------------


class TestReviewPathGating:
    def test_review_target_unset_review_path_not_entered(self) -> None:
        orch = _bare_orchestrator()
        _register_stub_runner("review-gate-stub-off")
        try:
            task = TaskConfig(
                description="d", steps=[TaskStep(name="s", runner="review-gate-stub-off")]
            )
            project = ProjectConfig(path=Path("/tmp/review-gate-project"))
            pipeline = PipelineConfig(description="d", stages=[], debate=DebateConfig())
            stage = PipelineStage(name="s", task="t")

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

            mock_review.assert_not_called()
            assert result == "ok"
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop("review-gate-stub-off", None)

    def test_review_target_set_review_path_entered(self, tmp_path: Path) -> None:
        orch = _bare_orchestrator()
        _register_stub_runner("review-gate-stub-on")
        try:
            task = TaskConfig(
                description="d", steps=[TaskStep(name="s", runner="review-gate-stub-on")]
            )
            # `_git_diff` (called before `_run_review`, mocked below) runs a
            # real `git diff` subprocess with this path as cwd -- must exist
            # on disk (need not be a git repo; a non-repo just returns None).
            project = ProjectConfig(path=tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(reviewers=["reviewer"], review_target="internal"),
            )
            stage = PipelineStage(name="s", task="t")

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch.object(orch, "_run_review") as mock_review,
            ):
                orch._execute_task_body(
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
            called_effective = mock_review.call_args.kwargs["effective"]
            assert called_effective.review_target == "internal"
            assert called_effective.reviewers == ["reviewer"]
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop("review-gate-stub-on", None)
