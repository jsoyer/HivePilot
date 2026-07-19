"""
Tests for Sprint 2 (auto-learning-lessons-loop PRD) — the `lessons` SQLite
table + APIs (`state_service.record_lesson`/`list_lessons`/
`mark_lesson_used`) and the opt-in, per-pipeline lesson distillation wired
into `Orchestrator._run_task_body` (`_distill_and_persist_lessons`, called
next to `knowledge_service.append_feedback` at the end of each project's
task run).

Covers:
- Distillation persists lessons, redacted, with `validated=False` and the
  distiller's self-reported score/confidence NEVER used as the persisted
  score/confidence (Sprint 3 computes those from real outcome signal).
- `settings.enable_lesson_distillation=False` -> `_distill_and_persist_
  lessons` is never called, zero `lessons` rows.
- Malformed/empty distiller output -> nothing persisted.
- `simulate=True` / `dry_run=True` -> no distillation, zero persistence
  (both gate the real LLM call, same as `simulate` gates every other
  `capture_definition` call site in `orchestrator.py`).
- `record_lesson` redacts BOTH `text` and `category` before INSERT (a
  direct API caller's unredacted `category` can't bypass masking).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.config import settings
from hivepilot.models import PipelineConfig, PipelineStage, ProjectConfig, TaskConfig, TaskStep
from hivepilot.services import config_provenance, state_service

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_verdict_run_correlation.py)
# ---------------------------------------------------------------------------


def _make_pipeline_by_name(*stage_names: str) -> PipelineConfig:
    stages = [PipelineStage(name=n, task=n) for n in stage_names]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator_with_pipeline(pipeline: PipelineConfig):
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

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


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


@pytest.fixture(autouse=True)
def _reset_distillation_flag() -> Iterator[None]:
    """Guarantee the opt-in flag never leaks between tests."""
    original = settings.enable_lesson_distillation
    yield
    settings.enable_lesson_distillation = original


# ---------------------------------------------------------------------------
# `_distill_and_persist_lessons` — direct unit coverage
# ---------------------------------------------------------------------------


class TestDistillAndPersistLessons:
    def _orch(self, capture_return: str) -> "hivepilot.orchestrator.Orchestrator":
        """Build an Orchestrator with a mocked registry whose
        `capture_definition` returns *capture_return*. The mock setup lives
        here (rather than in each test) so mypy's flow-sensitive narrowing
        sees `orch.registry` as `MagicMock` at the point `.return_value` is
        set -- narrowing doesn't survive an instance attribute round-tripping
        through a helper's return value."""
        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        mock_registry = MagicMock()
        mock_registry.capture_definition.return_value = capture_return
        orch.registry = mock_registry
        return orch

    def test_persists_lessons_redacted_with_validated_false(self) -> None:
        # Register a secret VALUE so `redact_text` has something to mask —
        # mirrors how a real run would have registered it via
        # `_resolve_secrets` earlier in the same run scope (S1's run-scope
        # masking, reused as-is here).
        config_provenance.register_secret_value("sk-live-supersecret")

        distilled_json = (
            '[{"text": "Never hardcode sk-live-supersecret in a fixture.", '
            '"category": "security", "source_verdict_id": null, '
            '"source_interaction_id": null, "score": 0.99, "confidence": 1.0}]'
        )
        orch = self._orch(distilled_json)
        project = ProjectConfig(path=Path("/tmp/lessons-p"))
        run_id = state_service.record_run_start("lessons-p", "my-task")
        # Real signal for this run — the no-signal short-circuit (verdicts
        # AND interactions both empty) must not swallow this call.
        state_service.record_verdict(
            run_id=run_id,
            project="lessons-p",
            task="my-task",
            role="developer",
            kind="debate",
            decision="adopt",
            confidence=0.8,
        )

        from hivepilot.orchestrator import RunResult

        orch._distill_and_persist_lessons(
            run_id=run_id,
            project=project,
            role="developer",
            task_name="my-task",
            result=RunResult("lessons-p", "developer", True, "ok"),
        )

        rows = state_service.list_lessons("lessons-p", validated_only=False)
        assert len(rows) == 1
        row = rows[0]
        assert row["run_id"] == run_id
        assert row["project"] == "lessons-p"
        assert row["role"] == "developer"
        assert row["task"] == "my-task"
        assert row["category"] == "security"
        # Redacted: the live secret value must never reach the persisted row.
        assert "sk-live-supersecret" not in row["text"]
        assert row["text"].startswith("Never hardcode")
        # Sprint 2 never sets validated=True, and the distiller's own
        # self-reported score/confidence (0.99 / 1.0 in the JSON above) must
        # NEVER be trusted as the persisted score/confidence.
        assert row["validated"] == 0
        assert row["score"] is None
        assert row["confidence"] is None

    def test_malformed_distiller_output_persists_nothing(self) -> None:
        orch = self._orch("not valid json at all")
        project = ProjectConfig(path=Path("/tmp/lessons-p2"))
        run_id = state_service.record_run_start("lessons-p2", "my-task")
        # Real signal so this exercises malformed-*response* parsing, not
        # the (separately tested) no-signal short-circuit.
        state_service.record_verdict(
            run_id=run_id,
            project="lessons-p2",
            task="my-task",
            role="developer",
            kind="debate",
            decision="adopt",
            confidence=0.8,
        )

        from hivepilot.orchestrator import RunResult

        orch._distill_and_persist_lessons(
            run_id=run_id,
            project=project,
            role="developer",
            task_name="my-task",
            result=RunResult("lessons-p2", "developer", True, "ok"),
        )

        assert state_service.list_lessons("lessons-p2", validated_only=False) == []

    def test_empty_distiller_output_persists_nothing(self) -> None:
        orch = self._orch("[]")
        project = ProjectConfig(path=Path("/tmp/lessons-p3"))
        run_id = state_service.record_run_start("lessons-p3", "my-task")
        # Real signal so this exercises the empty-array response parsing,
        # not the (separately tested) no-signal short-circuit.
        state_service.record_verdict(
            run_id=run_id,
            project="lessons-p3",
            task="my-task",
            role="developer",
            kind="debate",
            decision="adopt",
            confidence=0.8,
        )

        from hivepilot.orchestrator import RunResult

        orch._distill_and_persist_lessons(
            run_id=run_id,
            project=project,
            role="developer",
            task_name="my-task",
            result=RunResult("lessons-p3", "developer", True, "ok"),
        )

        assert state_service.list_lessons("lessons-p3", validated_only=False) == []

    def test_no_signal_skips_persistence_and_capture_call(self) -> None:
        """Both `verdicts` AND `interactions` empty -> `_distill_and_persist_
        lessons` never reaches `capture_fn` (mocked here as an
        exception-raiser to prove it's genuinely never invoked) and persists
        nothing, even with outcomes present (Sprint 2 review finding, LOW)."""
        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        mock_registry = MagicMock()
        mock_registry.capture_definition.side_effect = AssertionError(
            "capture_fn must not be called when there is no verdict/interaction signal"
        )
        orch.registry = mock_registry
        project = ProjectConfig(path=Path("/tmp/lessons-p7"))
        run_id = state_service.record_run_start("lessons-p7", "my-task")

        from hivepilot.orchestrator import RunResult

        orch._distill_and_persist_lessons(
            run_id=run_id,
            project=project,
            role="developer",
            task_name="my-task",
            result=RunResult("lessons-p7", "developer", True, "ok"),
        )

        assert state_service.list_lessons("lessons-p7", validated_only=False) == []

    def test_mark_lesson_used_increments_use_count(self) -> None:
        run_id = state_service.record_run_start("lessons-p4", "t")
        lesson_id = state_service.record_lesson(
            run_id=run_id,
            project="lessons-p4",
            role="developer",
            task="t",
            text="A lesson.",
            score=None,
            confidence=None,
            category="general",
        )
        state_service.mark_lesson_used(lesson_id)
        rows = state_service.list_lessons("lessons-p4", validated_only=False)
        assert rows[0]["use_count"] == 1

    def test_record_lesson_redacts_category_too(self) -> None:
        """Defense-in-depth symmetry fix (LOW): `record_lesson` redacted
        `text` but not `category` — a direct API caller (bypassing
        `distill_lessons`'s own prompt/response redaction) could pass an
        unredacted `category` containing a live secret and have it persist
        verbatim. Both free-text columns must go through the same choke
        point."""
        config_provenance.register_secret_value("sk-live-category-secret")
        try:
            run_id = state_service.record_run_start("lessons-p5", "t")
            lesson_id = state_service.record_lesson(
                run_id=run_id,
                project="lessons-p5",
                role="developer",
                task="t",
                text="Clean text.",
                score=None,
                confidence=None,
                category="leaked sk-live-category-secret in category",
            )
            rows = state_service.list_lessons("lessons-p5", validated_only=False)
            row = next(r for r in rows if r["id"] == lesson_id)
            assert "sk-live-category-secret" not in row["category"]
            assert "REDACTED" in row["category"]
        finally:
            config_provenance.clear_secret_values()

    def test_record_lesson_none_category_stays_none(self) -> None:
        """`category` is optional -- `None` must pass through unchanged, not
        crash `redact_text` or coerce to a string."""
        run_id = state_service.record_run_start("lessons-p6", "t")
        lesson_id = state_service.record_lesson(
            run_id=run_id,
            project="lessons-p6",
            role="developer",
            task="t",
            text="A lesson.",
            score=None,
            confidence=None,
            category=None,
        )
        rows = state_service.list_lessons("lessons-p6", validated_only=False)
        row = next(r for r in rows if r["id"] == lesson_id)
        assert row["category"] is None


# ---------------------------------------------------------------------------
# Orchestrator wiring — opt-in flag + simulate/dry_run gating
# ---------------------------------------------------------------------------


def _task_and_project():
    task = TaskConfig(
        description="t",
        engine="native",
        steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
    )
    project = ProjectConfig(path=Path("/tmp/wiring-p"))
    return task, project


class TestOrchestratorWiringGating:
    def _run(self, monkeypatch, *, simulate: bool, dry_run: bool) -> MagicMock:
        """Run `run_task` for one project/task with `_execute_task` stubbed
        out (never touches a real runner) and `_distill_and_persist_lessons`
        replaced with a spy, returning that spy for assertions."""
        task, project = _task_and_project()
        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.tasks.tasks = {"my-task": task}
        monkeypatch.setattr(orch, "_project", lambda name: project)
        monkeypatch.setattr(
            "hivepilot.orchestrator.policy_service.enforce_policy",
            lambda *a, **k: MagicMock(require_approval=False, block_on_severity=None),
        )
        monkeypatch.setattr(orch, "_execute_task", lambda **kwargs: "stubbed output")
        # `_collect_artifacts` shells out to `git diff` against the project
        # path -- irrelevant to this test's concern (the distillation gate)
        # and the fixture project path isn't a real git repo.
        monkeypatch.setattr(orch, "_collect_artifacts", lambda **kwargs: None)
        spy = MagicMock()
        monkeypatch.setattr(orch, "_distill_and_persist_lessons", spy)

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=99),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.knowledge_service.append_feedback"),
        ):
            orch.run_task(
                project_names=["wiring-p"],
                task_name="my-task",
                extra_prompt=None,
                auto_git=False,
                simulate=simulate,
                dry_run=dry_run,
            )
        return spy

    def test_flag_off_never_distills(self, monkeypatch) -> None:
        settings.enable_lesson_distillation = False
        spy = self._run(monkeypatch, simulate=False, dry_run=False)
        spy.assert_not_called()

    def test_flag_on_simulate_true_never_distills(self, monkeypatch) -> None:
        settings.enable_lesson_distillation = True
        spy = self._run(monkeypatch, simulate=True, dry_run=False)
        spy.assert_not_called()

    def test_flag_on_dry_run_true_never_distills(self, monkeypatch) -> None:
        settings.enable_lesson_distillation = True
        spy = self._run(monkeypatch, simulate=False, dry_run=True)
        spy.assert_not_called()

    def test_flag_on_not_simulate_not_dry_run_distills(self, monkeypatch) -> None:
        settings.enable_lesson_distillation = True
        spy = self._run(monkeypatch, simulate=False, dry_run=False)
        spy.assert_called_once()

    def test_distill_error_is_caught_and_does_not_break_pipeline(self, monkeypatch) -> None:
        """A raising `_distill_and_persist_lessons` must be caught -- the
        pipeline's own results must still come back successfully, same
        best-effort discipline as the Notion/Linear notification calls it
        sits next to."""
        task, project = _task_and_project()
        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        orch.tasks.tasks = {"my-task": task}
        monkeypatch.setattr(orch, "_project", lambda name: project)
        monkeypatch.setattr(
            "hivepilot.orchestrator.policy_service.enforce_policy",
            lambda *a, **k: MagicMock(require_approval=False, block_on_severity=None),
        )
        monkeypatch.setattr(orch, "_execute_task", lambda **kwargs: "stubbed output")
        monkeypatch.setattr(orch, "_collect_artifacts", lambda **kwargs: None)
        monkeypatch.setattr(
            orch,
            "_distill_and_persist_lessons",
            MagicMock(side_effect=RuntimeError("distiller blew up")),
        )
        settings.enable_lesson_distillation = True

        with (
            patch("hivepilot.orchestrator.state_service.record_run_start", return_value=99),
            patch("hivepilot.orchestrator.state_service.complete_run"),
            patch("hivepilot.orchestrator.knowledge_service.append_feedback"),
        ):
            results = orch.run_task(
                project_names=["wiring-p"],
                task_name="my-task",
                extra_prompt=None,
                auto_git=False,
                simulate=False,
                dry_run=False,
            )

        assert len(results) == 1
        assert results[0].success is True
