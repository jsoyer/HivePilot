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

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
    original_semantic_flag = settings.enable_semantic_lesson_retrieval
    yield
    settings.enable_lesson_distillation = original_flag
    settings.lesson_inject_limit = original_limit
    settings.enable_semantic_lesson_retrieval = original_semantic_flag


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

    def test_semantic_false_returns_plain_sqlite_ranking(self) -> None:
        """The dependency-free default path is now expressed as
        ``semantic=False`` (per-pipeline-lessons-yaml PRD made the
        ``semantic`` arg the ALREADY-RESOLVED decision -- `retrieve_lessons`
        no longer re-reads the raw `enable_semantic_lesson_retrieval` floor,
        so a resolved ``False`` is the sole "no re-rank" signal). It must
        return the deterministic SQLite score+recency ranking without any
        `mem0`/`FAISS`/`langchain` import."""
        _seed_validated_lesson(project="p", role="developer", task="t", text="low", score=0.5)
        _seed_validated_lesson(project="p", role="developer", task="t", text="high", score=0.9)
        lessons = retrieve_lessons("p", role="developer", task="t", limit=10, semantic=False)
        assert [lesson.text for lesson in lessons] == ["high", "low"]

    def test_semantic_true_flag_on_extras_absent_falls_back_no_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with the flag ON, a missing optional embedding extra (the
        default/common case -- `hivepilot[langchain]` not installed) must
        fall straight through to the SQLite ranking, never raise.

        `conftest.py` stubs `langchain_community` (and `.embeddings`) as
        MagicMocks in `sys.modules` for every test in this suite (so
        orchestrator-level tests can import without the real dependency) --
        that stub would falsely 'succeed' `_semantic_rerank`'s lazy import
        and return MagicMock junk instead of exercising the real
        `ImportError` path. Temporarily remove the stub entries so the
        import genuinely fails (the package really isn't installed in this
        test env), exercising `_semantic_rerank`'s own internal
        try/except-ImportError branch for real -- mirrors
        `test_knowledge_service.py`'s `_force_plain_context` pattern for the
        identical conftest-stub problem."""
        monkeypatch.delitem(sys.modules, "langchain_community", raising=False)
        monkeypatch.delitem(sys.modules, "langchain_community.embeddings", raising=False)
        settings.enable_semantic_lesson_retrieval = True
        try:
            _seed_validated_lesson(project="p", role="developer", task="t", text="low", score=0.5)
            _seed_validated_lesson(project="p", role="developer", task="t", text="high", score=0.9)
            lessons = retrieve_lessons("p", role="developer", task="t", limit=10, semantic=True)
            assert [lesson.text for lesson in lessons] == ["high", "low"]
        finally:
            settings.enable_semantic_lesson_retrieval = False

    def test_semantic_rerank_never_admits_unvalidated_candidate(self) -> None:
        """Semantic re-ranking must only ever reorder ALREADY-VALIDATED rows
        -- it must never surface an unvalidated candidate, flag on or off."""
        settings.enable_semantic_lesson_retrieval = True
        try:
            _seed_validated_lesson(
                project="p",
                role="developer",
                task="t",
                text="candidate-only",
                score=0.99,
                validated=False,
            )
            lessons = retrieve_lessons("p", role="developer", task="t", limit=10, semantic=True)
            assert lessons == []
        finally:
            settings.enable_semantic_lesson_retrieval = False


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
# build_lessons_context: the semantic flag must actually REACH the
# production injection path (Sprint 4 adversarial-sweep fix -- previously
# `build_lessons_context` called `retrieve_lessons(...)` without
# `semantic=...`, so `enable_semantic_lesson_retrieval` had ZERO effect on
# real injected lessons even though it was documented as re-ranking them).
# ---------------------------------------------------------------------------


class TestBuildLessonsContextSemanticFlagWiring:
    def test_semantic_flag_on_reaches_semantic_rerank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hivepilot.services.lessons_service as lessons_service_module

        settings.enable_lesson_distillation = True
        settings.enable_semantic_lesson_retrieval = True
        try:
            spy = MagicMock(return_value=None)  # None -> falls back, doesn't alter ranking
            monkeypatch.setattr(lessons_service_module, "_semantic_rerank", spy)
            _seed_validated_lesson(
                project="p", role="developer", task="t", text="Semantic-eligible lesson.", score=0.9
            )

            text = build_lessons_context("p", "developer", "t")

            assert spy.called, (
                "enable_semantic_lesson_retrieval=True must reach "
                "_semantic_rerank via the production build_lessons_context path"
            )
            # The fallback (spy returns None) must still surface the lesson --
            # this flag can only ever add re-ranking, never lose a lesson.
            assert "Semantic-eligible lesson." in text
        finally:
            settings.enable_semantic_lesson_retrieval = False

    def test_semantic_flag_off_never_reaches_semantic_rerank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hivepilot.services.lessons_service as lessons_service_module

        settings.enable_lesson_distillation = True
        settings.enable_semantic_lesson_retrieval = False
        spy = MagicMock(return_value=None)
        monkeypatch.setattr(lessons_service_module, "_semantic_rerank", spy)
        _seed_validated_lesson(
            project="p", role="developer", task="t", text="SQLite-only lesson.", score=0.9
        )

        text = build_lessons_context("p", "developer", "t")

        assert not spy.called, (
            "enable_semantic_lesson_retrieval=False (the default) must never "
            "reach _semantic_rerank, even when build_lessons_context passes "
            "semantic=False through explicitly"
        )
        assert "SQLite-only lesson." in text

    def test_pipeline_semantic_override_reaches_rerank_with_floor_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (per-pipeline-lessons-yaml follow-up): a per-pipeline
        ``lessons.enable_semantic=True`` must REACH `_semantic_rerank` even
        when the global `enable_semantic_lesson_retrieval` floor is OFF --
        previously `retrieve_lessons` re-gated on the raw floor, making the
        strengthen-only override inert. The resolved `enable_semantic` is now
        authoritative end-to-end through `build_lessons_context`."""
        import hivepilot.services.lessons_service as lessons_service_module
        from hivepilot.models import (
            LessonsConfig,
            PipelineConfig,
            resolve_lessons_config,
        )

        settings.enable_lesson_distillation = True
        settings.enable_semantic_lesson_retrieval = False  # floor OFF
        resolved = resolve_lessons_config(
            pipeline=PipelineConfig(description="d", lessons=LessonsConfig(enable_semantic=True))
        )
        assert resolved.enable_semantic is True  # strengthen-only OR

        spy = MagicMock(return_value=None)  # None -> falls back, keeps ranking
        monkeypatch.setattr(lessons_service_module, "_semantic_rerank", spy)
        _seed_validated_lesson(
            project="p", role="developer", task="t", text="Override lesson.", score=0.9
        )

        text = build_lessons_context("p", "developer", "t", effective=resolved)

        assert spy.called, (
            "per-pipeline enable_semantic=True must reach _semantic_rerank "
            "even with the global floor OFF"
        )
        assert "Override lesson." in text  # fallback still surfaces the lesson

    def test_retrieve_lessons_called_with_flag_value_as_semantic_kwarg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct proof of the exact wiring fix: `build_lessons_context`
        passes `semantic=settings.enable_semantic_lesson_retrieval` into
        `retrieve_lessons` -- not a hardcoded `False`/omitted kwarg."""
        import hivepilot.services.lessons_service as lessons_service_module

        settings.enable_lesson_distillation = True
        real_retrieve_lessons = lessons_service_module.retrieve_lessons
        calls: list[dict[str, Any]] = []

        def _spy_retrieve_lessons(*args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return real_retrieve_lessons(*args, **kwargs)

        monkeypatch.setattr(lessons_service_module, "retrieve_lessons", _spy_retrieve_lessons)

        for flag_value in (True, False):
            settings.enable_semantic_lesson_retrieval = flag_value
            try:
                calls.clear()
                build_lessons_context("p", "developer", "t")
                assert len(calls) == 1
                assert calls[0]["semantic"] is flag_value
            finally:
                settings.enable_semantic_lesson_retrieval = False


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
