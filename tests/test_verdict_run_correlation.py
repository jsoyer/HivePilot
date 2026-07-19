"""
Tests for Sprint 1 (auto-learning-lessons-loop PRD) — threading a real
``run_id`` (+ ``task``) into the debate-judge ``record_verdict`` persistence
call.

Before this sprint, ``Orchestrator._run_debate_body``'s judge-verdict
persistence call (``hivepilot/orchestrator.py``, the ``kind="debate"``
``state_service.record_verdict(...)`` call) hardcoded ``run_id=None,
task=None`` even when the debate was role-driven from inside a real pipeline
run (``_execute_task_body``'s dual-model-debate early-return branch already
has both values in scope — see ``run_id``/``task_name`` params). That made
every persisted debate verdict impossible to correlate back to the run/task
that produced it — the same class of gap
``Orchestrator._persist_challenge_verdict`` (the CHALLENGE-arbiter sibling
path) already closed by accepting a real ``run_id`` from its caller
(``_resolve_challenge_via_arbiter``).

Covers:
- ``Orchestrator.run_debate(..., run_id=X, task_name=Y)`` persists the judge
  verdict via ``state_service.record_verdict(run_id=X, task=Y, ...)``.
- Standalone callers that don't supply ``run_id``/``task_name`` (cli.py's
  ``debate`` command, the ChatOps daemon) keep persisting
  ``run_id=None, task=None`` — byte-identical to pre-Sprint-1 behaviour, so
  this is additive, not a breaking contract change.
- The dual-model-debate task path inside ``_execute_task_body`` threads the
  REAL run/task context into ``run_debate`` — not left at the defaults.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.config import settings
from hivepilot.models import PipelineConfig, PipelineStage
from hivepilot.services import config_provenance

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_debate_judge.py)
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


class _FakeDebate:
    """Stands in for `DebateService` — never touches a real vault."""

    def __init__(self, vault, dry_run=True) -> None:
        pass

    def run(self, topic, positions, decision=None, confidence=None, **kw):
        return {"path": "ADR.md", "dry_run": True}


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


@pytest.fixture(autouse=True)
def _reset_judge_flag() -> Iterator[None]:
    """Guarantee the opt-in flag never leaks between tests."""
    original = settings.enable_debate_judge
    yield
    settings.enable_debate_judge = original


def _orch_ready_for_judge_debate(monkeypatch) -> "hivepilot.orchestrator.Orchestrator":
    from hivepilot.models import ProjectConfig

    orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
    orch.registry = MagicMock()
    monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
    monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
    monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)
    monkeypatch.setattr(settings, "enable_debate_judge", True)

    judge_json = '{"decision": "Adopt plan X.", "confidence": 0.9}'
    orch.registry.capture_definition.side_effect = [
        "brain one output",
        "brain two output",
        judge_json,
    ]
    return orch


# ---------------------------------------------------------------------------
# run_debate -> record_verdict threading
# ---------------------------------------------------------------------------


class TestRunDebatePersistsRealRunId:
    def test_run_id_and_task_supplied_are_persisted_on_the_verdict(self, monkeypatch) -> None:
        orch = _orch_ready_for_judge_debate(monkeypatch)

        with (
            patch("hivepilot.orchestrator.state_service.record_interaction"),
            patch("hivepilot.orchestrator.state_service.record_verdict") as mock_record,
        ):
            orch.run_debate(
                project_name="p",
                role_name="ceo",
                topic="adopt X?",
                simulate=False,
                run_id=42,
                task_name="pipeline-stage-x",
            )

        assert mock_record.call_count == 1
        _, kwargs = mock_record.call_args
        assert kwargs["run_id"] == 42
        assert kwargs["task"] == "pipeline-stage-x"
        assert kwargs["kind"] == "debate"
        assert kwargs["role"] == "ceo"

    def test_standalone_call_without_run_id_still_persists_none(self, monkeypatch) -> None:
        """cli.py's `debate` command / the ChatOps daemon never supply
        `run_id`/`task_name` — must remain byte-identical to pre-Sprint-1
        behaviour (no crash, no fabricated run correlation)."""
        orch = _orch_ready_for_judge_debate(monkeypatch)

        with (
            patch("hivepilot.orchestrator.state_service.record_interaction"),
            patch("hivepilot.orchestrator.state_service.record_verdict") as mock_record,
        ):
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=False)

        assert mock_record.call_count == 1
        _, kwargs = mock_record.call_args
        assert kwargs["run_id"] is None
        assert kwargs["task"] is None


# ---------------------------------------------------------------------------
# _execute_task_body's dual-model-debate branch threads the REAL run context
# ---------------------------------------------------------------------------


class TestExecuteTaskThreadsRunContextIntoRunDebate:
    def test_execute_task_passes_real_run_id_and_task_name_to_run_debate(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})

        fake_role = MagicMock(models=["claude:model-a", "claude:model-b"], model=None)
        monkeypatch.setattr("hivepilot.roles.get_role", lambda name: fake_role)

        captured: dict = {}

        def _fake_run_debate(**kwargs):
            captured.update(kwargs)
            return {"path": "ADR.md"}

        monkeypatch.setattr(orch, "run_debate", _fake_run_debate)

        task = TaskConfig(
            description="t",
            role="ceo",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))

        with patch("hivepilot.orchestrator.state_service.record_step"):
            orch._execute_task(
                project=project,
                task_name="my-debate-task",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=77,
                dry_run=True,
                simulate=True,
            )

        assert captured["run_id"] == 77
        assert captured["task_name"] == "my-debate-task"
        assert captured["role_name"] == "ceo"

    def test_execute_task_passes_none_run_id_when_no_run_context(self, monkeypatch) -> None:
        """A plain (non-pipeline) task run with no `run_id` must not fabricate
        one — `run_debate` receives `run_id=None` exactly as it received
        before this sprint's threading existed."""
        from hivepilot.models import ProjectConfig, TaskConfig, TaskStep

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})

        fake_role = MagicMock(models=["claude:model-a", "claude:model-b"], model=None)
        monkeypatch.setattr("hivepilot.roles.get_role", lambda name: fake_role)

        captured: dict = {}

        def _fake_run_debate(**kwargs):
            captured.update(kwargs)
            return {"path": "ADR.md"}

        monkeypatch.setattr(orch, "run_debate", _fake_run_debate)

        task = TaskConfig(
            description="t",
            role="ceo",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=Path("/tmp/p"))

        orch._execute_task(
            project=project,
            task_name="my-debate-task",
            task=task,
            extra_prompt=None,
            auto_git=False,
            run_id=None,
            dry_run=True,
            simulate=True,
        )

        assert captured["run_id"] is None
