"""
Unit tests for `lessons_service.retrieve_lessons` (ranking/keying/cap) and
its wiring into the "Lessons learned" prompt section
(`knowledge_service.build_lessons_context`, consumed by both
`ClaudeRunner._build_prompt` and `PromptCliRunner._augment_prompt`) --
Auto-Learning Lessons Loop PRD, Sprint 3.

Covers:
- `retrieve_lessons`: validated-only, ranked score DESC then recency DESC,
  keyed project:role:task, capped at `limit`.
- `mark_lesson_used` is called (use_count incremented) for every lesson
  actually injected.
- Unvalidated candidates NEVER appear in the built prompt.
- The injected section is wired into BOTH runners, next to (not inside)
  the stable `Knowledge context` block.
- `settings.enable_lesson_distillation=False` -> the prompt is
  BYTE-IDENTICAL to the pre-Sprint-3 prompt, even with validated lessons
  sitting in the DB -- proven by capturing the exact same payload/
  instructions before and after seeding validated lessons, flag held off
  throughout.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.prompt_cli_runner import PromptCliRunner, VibeRunner
from hivepilot.services import state_service
from hivepilot.services.knowledge_service import build_lessons_context
from hivepilot.services.lessons_service import retrieve_lessons


@pytest.fixture(autouse=True)
def _reset_lesson_settings() -> Iterator[None]:
    """Guarantee the opt-in flag + inject limit never leak between tests."""
    original_flag = settings.enable_lesson_distillation
    original_limit = settings.lesson_inject_limit
    yield
    settings.enable_lesson_distillation = original_flag
    settings.lesson_inject_limit = original_limit


def _seed_validated_lesson(
    *,
    project: str,
    role: str | None,
    task: str | None,
    text: str,
    score: float,
    validated: bool = True,
) -> int:
    """Persist a lesson row and (optionally) validate it, mirroring the
    real `_distill_and_persist_lessons` -> `validate_lesson` ->
    `update_lesson_validation` sequence -- but with a caller-chosen score
    instead of computing one, so ranking tests can pin exact values."""
    run_id = state_service.record_run_start(project, task or "t")
    lesson_id = state_service.record_lesson(
        run_id=run_id,
        project=project,
        role=role,
        task=task,
        text=text,
        score=None,
        confidence=None,
        category="general",
        validated=False,
    )
    if validated:
        state_service.update_lesson_validation(lesson_id, validated=True, score=score)
    return lesson_id


# ---------------------------------------------------------------------------
# retrieve_lessons: ranking / keying / cap / validated-only
# ---------------------------------------------------------------------------


class TestRetrieveLessonsRanking:
    def test_ranked_by_score_desc(self) -> None:
        _seed_validated_lesson(project="p", role="developer", task="t", text="low", score=0.5)
        _seed_validated_lesson(project="p", role="developer", task="t", text="high", score=0.9)
        lessons = retrieve_lessons("p", role="developer", task="t", limit=10)
        assert [lesson.text for lesson in lessons] == ["high", "low"]

    def test_recency_tiebreak_within_same_score(self) -> None:
        _seed_validated_lesson(project="p", role="developer", task="t", text="older", score=0.8)
        _seed_validated_lesson(project="p", role="developer", task="t", text="newer", score=0.8)
        lessons = retrieve_lessons("p", role="developer", task="t", limit=10)
        # Same score -> most recently inserted (newer) ranks first.
        assert [lesson.text for lesson in lessons] == ["newer", "older"]

    def test_keyed_by_project_role_task(self) -> None:
        _seed_validated_lesson(project="p", role="developer", task="t", text="match", score=0.9)
        _seed_validated_lesson(
            project="p", role="other-role", task="t", text="wrong-role", score=0.9
        )
        _seed_validated_lesson(
            project="p", role="developer", task="other-task", text="wrong-task", score=0.9
        )
        _seed_validated_lesson(
            project="other-project", role="developer", task="t", text="wrong-project", score=0.9
        )
        lessons = retrieve_lessons("p", role="developer", task="t", limit=10)
        assert [lesson.text for lesson in lessons] == ["match"]

    def test_capped_at_limit(self) -> None:
        for i in range(10):
            _seed_validated_lesson(
                project="p", role="developer", task="t", text=f"lesson-{i}", score=0.5 + i * 0.01
            )
        lessons = retrieve_lessons("p", role="developer", task="t", limit=3)
        assert len(lessons) == 3
        # Highest-score three, in descending order.
        assert [lesson.text for lesson in lessons] == ["lesson-9", "lesson-8", "lesson-7"]

    def test_unvalidated_never_retrieved(self) -> None:
        _seed_validated_lesson(
            project="p",
            role="developer",
            task="t",
            text="candidate-only",
            score=0.99,
            validated=False,
        )
        lessons = retrieve_lessons("p", role="developer", task="t", limit=10)
        assert lessons == []

    def test_semantic_true_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            retrieve_lessons("p", semantic=True)


# ---------------------------------------------------------------------------
# build_lessons_context: gate + mark_lesson_used
# ---------------------------------------------------------------------------


class TestBuildLessonsContext:
    def test_flag_off_returns_empty_even_with_validated_lessons(self) -> None:
        settings.enable_lesson_distillation = False
        _seed_validated_lesson(project="p", role="developer", task="t", text="lesson", score=0.9)
        assert build_lessons_context("p", "developer", "t") == ""

    def test_flag_on_no_lessons_returns_empty(self) -> None:
        settings.enable_lesson_distillation = True
        assert build_lessons_context("p", "developer", "t") == ""

    def test_flag_on_returns_formatted_lessons_and_marks_used(self) -> None:
        settings.enable_lesson_distillation = True
        lesson_id = _seed_validated_lesson(
            project="p", role="developer", task="t", text="Run tests first.", score=0.9
        )
        text = build_lessons_context("p", "developer", "t")
        assert "Run tests first." in text
        row = state_service.list_lessons("p", validated_only=False)
        matched = next(r for r in row if r["id"] == lesson_id)
        assert matched["use_count"] == 1

    def test_flag_on_unvalidated_lesson_never_in_context(self) -> None:
        settings.enable_lesson_distillation = True
        _seed_validated_lesson(
            project="p",
            role="developer",
            task="t",
            text="Should never appear.",
            score=0.99,
            validated=False,
        )
        assert build_lessons_context("p", "developer", "t") == ""

    def test_respects_inject_limit(self) -> None:
        settings.enable_lesson_distillation = True
        settings.lesson_inject_limit = 2
        for i in range(5):
            _seed_validated_lesson(
                project="p", role="developer", task="t", text=f"lesson-{i}", score=0.5 + i * 0.01
            )
        text = build_lessons_context("p", "developer", "t")
        assert text.count("lesson-") == 2


# ---------------------------------------------------------------------------
# Runner wiring: ClaudeRunner._build_prompt / PromptCliRunner._augment_prompt
# ---------------------------------------------------------------------------


def _claude_payload(tmp_path: Path, *, role: str | None, task_name: str = "t") -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name=task_name,
        step=TaskStep(name="s", runner="claude"),
        metadata={"role": role} if role is not None else {},
        secrets={},
    )


def _claude_runner() -> ClaudeRunner:
    return ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), settings)


def _cli_payload(tmp_path: Path, *, role: str | None, task_name: str = "t") -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name=task_name,
        step=TaskStep(name="s", runner="api"),
        metadata={"role": role} if role is not None else {},
        secrets={},
    )


def _cli_runner() -> PromptCliRunner:
    return VibeRunner(RunnerDefinition(name="cli", kind="vibe", command="vibe"), settings)


class TestClaudeRunnerLessonsWiring:
    def test_validated_lesson_injected_next_to_knowledge_context(self, tmp_path: Path) -> None:
        settings.enable_lesson_distillation = True
        _seed_validated_lesson(
            project="p", role="developer", task="t", text="Never skip tests.", score=0.9
        )
        payload = _claude_payload(tmp_path, role="developer")
        out = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)
        assert "Lessons learned:" in out
        assert "Never skip tests." in out
        # Lessons learned must appear before the volatile "Instructions:"
        # section (stable-first ordering, same discipline as Knowledge context).
        assert out.index("Lessons learned:") < out.index("Instructions:")

    def test_flag_off_prompt_byte_identical(self, tmp_path: Path) -> None:
        settings.enable_lesson_distillation = False
        payload = _claude_payload(tmp_path, role="developer")
        baseline = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)

        # Seed a validated lesson while the flag stays off.
        _seed_validated_lesson(
            project="p", role="developer", task="t", text="Should not appear.", score=0.9
        )
        after_seed = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)

        assert after_seed == baseline
        assert "Lessons learned" not in baseline
        assert "Should not appear." not in after_seed

    def test_missing_role_degrades_instead_of_crashing(self, tmp_path: Path) -> None:
        """No 'role' key in metadata (a payload built by a call site that
        predates this Sprint, or a non-role task) must not crash --
        retrieval degrades to project+task keying."""
        settings.enable_lesson_distillation = True
        _seed_validated_lesson(project="p", role=None, task="t", text="No-role lesson.", score=0.9)
        payload = _claude_payload(tmp_path, role=None)
        out = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)
        assert "No-role lesson." in out


class TestPromptCliRunnerLessonsWiring:
    def test_validated_lesson_injected_next_to_knowledge_context(self, tmp_path: Path) -> None:
        settings.enable_lesson_distillation = True
        _seed_validated_lesson(
            project="p", role="developer", task="t", text="Never skip tests.", score=0.9
        )
        payload = _cli_payload(tmp_path, role="developer")
        out = _cli_runner()._augment_prompt(payload, "BASE_INSTRUCTIONS")
        assert "Lessons learned:" in out
        assert "Never skip tests." in out
        assert out.index("Lessons learned:") < out.index("Instructions:")

    def test_flag_off_prompt_byte_identical(self, tmp_path: Path) -> None:
        settings.enable_lesson_distillation = False
        payload = _cli_payload(tmp_path, role="developer")
        baseline = _cli_runner()._augment_prompt(payload, "BASE_INSTRUCTIONS")

        _seed_validated_lesson(
            project="p", role="developer", task="t", text="Should not appear.", score=0.9
        )
        after_seed = _cli_runner()._augment_prompt(payload, "BASE_INSTRUCTIONS")

        assert after_seed == baseline
        # With nothing else stable/volatile present, the flag-off prompt is
        # exactly the base instructions untouched.
        assert baseline == "BASE_INSTRUCTIONS"

    def test_unvalidated_lesson_never_injected(self, tmp_path: Path) -> None:
        settings.enable_lesson_distillation = True
        _seed_validated_lesson(
            project="p",
            role="developer",
            task="t",
            text="Candidate only, not validated.",
            score=0.99,
            validated=False,
        )
        payload = _cli_payload(tmp_path, role="developer")
        out = _cli_runner()._augment_prompt(payload, "BASE_INSTRUCTIONS")
        assert "Candidate only, not validated." not in out
        assert out == "BASE_INSTRUCTIONS"
