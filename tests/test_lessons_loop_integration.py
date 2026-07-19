"""
Cross-cutting integration tests for the Auto-Learning Lessons Loop PRD
(Sprint 4) -- exercises the FULL distill -> validate -> inject chain, plus
the redaction guarantee and the opt-in semantic fallback, end to end across
`hivepilot.orchestrator`, `hivepilot.services.lessons_service`,
`hivepilot.services.state_service`, `hivepilot.services.knowledge_service`,
and the `mem0`/`obsidian` plugins -- rather than each module's own unit
tests in isolation (`tests/test_lessons_distillation.py`,
`tests/test_lessons_validation.py`, `tests/test_lessons_injection.py`,
`tests/test_mem0.py`, `tests/test_plugin_obsidian.py`).

Only the LLM boundary is mocked (`RunnerRegistry.capture_definition` /
the distiller's `capture_fn`) -- every other layer (SQLite persistence,
validation gate, prompt injection, redaction, plugin hooks) runs for real.

Covers:
- End-to-end: a successful run's verdicts/interactions distill into a
  candidate, the REAL outcome signal validates it, and it appears in a
  SUBSEQUENT run's rendered prompt via `ClaudeRunner._build_prompt`'s
  stable "Lessons learned:" section.
- A failed run's distilled candidate is persisted but QUARANTINED
  (`validated=0`) and never reaches `build_lessons_context`/a rendered
  prompt.
- A `${secret:...}`-resolved value present in a run's outputs/detail never
  reaches: the persisted `lessons` table text, the prompt sent to the
  distiller's `capture_fn`, or the `mem0`/`obsidian` `store()` hook args.
- `enable_semantic_lesson_retrieval=True` with the optional embedding
  extra unavailable never crashes `retrieve_lessons(semantic=True)` --
  falls back to the SQLite-ranked validated lessons.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.orchestrator  # noqa: F401 -- side-effect import for patch resolution
from hivepilot.config import settings
from hivepilot.models import (
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskStep,
)
from hivepilot.orchestrator import RunResult
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.services import config_provenance, state_service
from hivepilot.services.knowledge_service import build_lessons_context
from hivepilot.services.lessons_service import retrieve_lessons

REPO_ROOT = Path(__file__).parent.parent
MEM0_PLUGIN_PATH = REPO_ROOT / "plugins" / "mem0.py"
OBSIDIAN_PLUGIN_PATH = REPO_ROOT / "plugins" / "obsidian.py"


def _load_plugin_module(path: Path, name: str) -> ModuleType:
    """Load a local-file plugin by path -- same mechanism
    `hivepilot.plugins._scan_local_plugins` uses. Mirrors
    `tests/test_mem0.py::_load_mem0_module` / `tests/test_plugin_obsidian.
    py::_load_obsidian_module`."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Shared orchestrator/settings scaffolding (mirrors
# tests/test_lessons_distillation.py's helpers).
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


def _orch_with_capture(capture_return: str | MagicMock):
    """Build an Orchestrator whose `registry.capture_definition` returns/
    behaves per *capture_return* -- a plain string return value, or a
    `MagicMock` already configured with `side_effect`/`return_value` by the
    caller (used by the secret-leak test to inspect the actual call args)."""
    orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
    mock_registry = MagicMock()
    if isinstance(capture_return, str):
        mock_registry.capture_definition.return_value = capture_return
    else:
        mock_registry.capture_definition = capture_return
    orch.registry = mock_registry
    return orch


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


@pytest.fixture(autouse=True)
def _reset_lesson_settings() -> Iterator[None]:
    """Guarantee every opt-in flag this loop reads never leaks between
    tests -- mirrors the equivalent fixtures in `tests/test_lessons_
    distillation.py` / `tests/test_lessons_injection.py`."""
    original_distill = settings.enable_lesson_distillation
    original_semantic = settings.enable_semantic_lesson_retrieval
    original_limit = settings.lesson_inject_limit
    yield
    settings.enable_lesson_distillation = original_distill
    settings.enable_semantic_lesson_retrieval = original_semantic
    settings.lesson_inject_limit = original_limit


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


# ---------------------------------------------------------------------------
# End-to-end: distill -> validate -> inject
# ---------------------------------------------------------------------------


class TestDistillValidateInjectEndToEnd:
    def test_repeated_pipeline_distills_validates_and_injects_lesson(self, tmp_path: Path) -> None:
        settings.enable_lesson_distillation = True
        distilled_json = (
            '[{"text": "Run migrations before seeding fixtures.", '
            '"category": "testing", "source_verdict_id": null, '
            '"source_interaction_id": null}]'
        )
        orch = _orch_with_capture(distilled_json)
        project = ProjectConfig(path=tmp_path / "p")
        run_id = state_service.record_run_start("p", "t")
        # Real signal -- a genuinely ACCEPTed challenge verdict at/above the
        # floor, so `_build_lesson_outcome_signal` treats it as resolved
        # (belt-and-suspenders on top of `RunResult.success=True` alone).
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="developer",
            kind="challenge",
            decision="ACCEPT",
            confidence=0.9,
        )

        # 1) Run N: distill + persist + validate off a successful outcome.
        orch._distill_and_persist_lessons(
            run_id=run_id,
            project=project,
            role="developer",
            task_name="t",
            result=RunResult("p", "developer", True, "ok"),
        )

        rows = state_service.list_lessons("p", validated_only=True)
        assert len(rows) == 1
        assert rows[0]["text"] == "Run migrations before seeding fixtures."
        assert rows[0]["validated"] == 1

        # 2) Run N+1: the SAME validated lesson must be retrievable...
        retrieved = retrieve_lessons("p", role="developer", task="t", limit=5)
        assert [lesson.text for lesson in retrieved] == ["Run migrations before seeding fixtures."]

        # ...and reach the "Lessons learned:" stable section of a freshly
        # rendered prompt (`ClaudeRunner._build_prompt`), next to (not
        # inside) the "Knowledge context"/"Instructions:" sections.
        context = build_lessons_context("p", "developer", "t")
        assert "Run migrations before seeding fixtures." in context

        payload = _claude_payload(tmp_path, role="developer")
        prompt = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)
        assert "Lessons learned:" in prompt
        assert "Run migrations before seeding fixtures." in prompt
        assert prompt.index("Lessons learned:") < prompt.index("Instructions:")


# ---------------------------------------------------------------------------
# Failed-run lesson quarantine
# ---------------------------------------------------------------------------


class TestFailedRunLessonQuarantined:
    def test_failed_run_lesson_persisted_but_never_validated_or_injected(
        self, tmp_path: Path
    ) -> None:
        settings.enable_lesson_distillation = True
        distilled_json = (
            '[{"text": "This lesson came from a run that actually failed.", '
            '"category": "general", "source_verdict_id": null, '
            '"source_interaction_id": null}]'
        )
        orch = _orch_with_capture(distilled_json)
        project = ProjectConfig(path=tmp_path / "p")
        run_id = state_service.record_run_start("p", "t")
        # Real signal so distillation isn't skipped by the no-signal
        # short-circuit -- but a REJECTED challenge (not an ACCEPT), so it
        # contributes NOTHING to the outcome signal (fail-closed contract).
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="developer",
            kind="challenge",
            decision="MAINTAIN",
            confidence=0.95,
        )

        orch._distill_and_persist_lessons(
            run_id=run_id,
            project=project,
            role="developer",
            task_name="t",
            result=RunResult("p", "developer", False, "step failed"),
        )

        # Persisted (candidate exists)...
        all_rows = state_service.list_lessons("p", validated_only=False)
        assert len(all_rows) == 1
        assert all_rows[0]["validated"] == 0
        assert all_rows[0]["score"] == 0.0

        # ...but QUARANTINED: never surfaced by validated-only retrieval,
        # never in the injected prompt context.
        assert state_service.list_lessons("p", validated_only=True) == []
        assert retrieve_lessons("p", role="developer", task="t", limit=5) == []
        assert build_lessons_context("p", "developer", "t") == ""

        payload = _claude_payload(tmp_path, role="developer")
        prompt = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)
        assert "This lesson came from a run that actually failed." not in prompt


# ---------------------------------------------------------------------------
# Secret never leaks through the loop
# ---------------------------------------------------------------------------


_SECRET_VALUE = "sk-live-integration-secret-abcXYZ123"


class TestSecretNeverLeaksThroughLoop:
    def test_secret_never_in_distiller_prompt_or_lessons_table(self, tmp_path: Path) -> None:
        """A resolved `${secret:NAME}` value sitting in `RunResult.detail`
        (the choke point `lessons_service.distill_lessons`'s own docstring
        calls out -- `outcomes[].detail` is NOT pre-redacted upstream like
        verdict/interaction summaries are) must never reach the prompt sent
        to the distiller's `capture_fn`, nor the persisted `lessons` row."""
        config_provenance.register_secret_value(_SECRET_VALUE)

        captured_payloads: list[RunnerPayload] = []

        def _fake_capture(_definition: object, payload: RunnerPayload) -> str:
            captured_payloads.append(payload)
            return (
                '[{"text": "Handle failures gracefully.", "category": "general", '
                '"source_verdict_id": null, "source_interaction_id": null}]'
            )

        mock_capture = MagicMock(side_effect=_fake_capture)
        orch = _orch_with_capture(mock_capture)
        project = ProjectConfig(path=tmp_path / "p")
        run_id = state_service.record_run_start("p", "t")
        state_service.record_verdict(
            run_id=run_id,
            project="p",
            task="t",
            role="developer",
            kind="challenge",
            decision="ACCEPT",
            confidence=0.9,
        )

        # The secret leaks into RunResult.detail exactly the way
        # `RunResult`'s own choke-point comment (hivepilot/orchestrator.py)
        # warns it can -- a step's failure/output detail echoing a resolved
        # secret verbatim.
        leaking_detail = f"step failed while using token {_SECRET_VALUE}"

        orch._distill_and_persist_lessons(
            run_id=run_id,
            project=project,
            role="developer",
            task_name="t",
            result=RunResult("p", "developer", True, leaking_detail),
        )

        # 1) The prompt actually sent to the distiller's capture_fn.
        assert len(captured_payloads) == 1
        sent_prompt = captured_payloads[0].metadata.get("extra_prompt") or ""
        assert _SECRET_VALUE not in sent_prompt

        # 2) The persisted lessons table.
        rows = state_service.list_lessons("p", validated_only=False)
        assert len(rows) == 1
        assert _SECRET_VALUE not in rows[0]["text"]

    def test_secret_never_in_mem0_store_args(self, tmp_path: Path) -> None:
        config_provenance.register_secret_value(_SECRET_VALUE)
        mem0_module = _load_plugin_module(MEM0_PLUGIN_PATH, "hivepilot_plugin_mem0_integration")

        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude"),
            metadata={"extra_prompt": f"use token {_SECRET_VALUE} to auth"},
            secrets={"API_TOKEN": _SECRET_VALUE},
        )
        mock_client = MagicMock()

        with (
            patch.object(settings, "mem0_enabled", True),
            patch.object(mem0_module, "_get_client", return_value=mock_client),
        ):
            mem0_module.store(
                payload=payload,
                role="developer",
                output=f"result used {_SECRET_VALUE}",
            )

        assert mock_client.add.called
        content = mock_client.add.call_args.args[0]
        metadata = mock_client.add.call_args.kwargs["metadata"]
        assert _SECRET_VALUE not in content
        assert not any(_SECRET_VALUE in str(v) for v in metadata.values())

    def test_secret_never_in_obsidian_store_args(self, tmp_path: Path) -> None:
        config_provenance.register_secret_value(_SECRET_VALUE)
        obsidian_module = _load_plugin_module(
            OBSIDIAN_PLUGIN_PATH, "hivepilot_plugin_obsidian_integration"
        )

        vault = tmp_path / "Vault"
        (vault / "12 - HivePilot" / "Runs").mkdir(parents=True)

        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude"),
            metadata={},
            secrets={},
        )

        written: list[str] = []

        with (
            patch.object(settings, "obsidian_enabled", True),
            patch.object(settings, "obsidian_vault", vault),
            patch.object(
                obsidian_module.ObsidianService,
                "append_daily",
                side_effect=lambda self, entry: written.append(entry),
                autospec=True,
            ),
        ):
            obsidian_module.store(
                payload=payload,
                role="developer",
                output=f"leaked value: {_SECRET_VALUE}",
            )

        assert written, "store() must have appended an entry"
        assert not any(_SECRET_VALUE in entry for entry in written)


# ---------------------------------------------------------------------------
# Semantic fallback with the optional embedding extra absent
# ---------------------------------------------------------------------------


class TestSemanticFallbackWithExtrasAbsent:
    def test_semantic_true_extras_unavailable_falls_back_no_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`enable_semantic_lesson_retrieval=True` but the optional
        `hivepilot[langchain]` extra genuinely unavailable (the common/
        default case) -- `retrieve_lessons(semantic=True)` must return the
        SQLite-ranked validated lessons, never raise. `conftest.py` stubs
        `langchain_community` as a MagicMock for every test in this suite;
        temporarily remove the stub so the import genuinely fails, exactly
        as it would on a real install without the extra (same technique as
        `tests/test_lessons_injection.py`'s equivalent unit test and
        `tests/test_knowledge_service.py`'s `_force_plain_context`)."""
        monkeypatch.delitem(sys.modules, "langchain_community", raising=False)
        monkeypatch.delitem(sys.modules, "langchain_community.embeddings", raising=False)

        run_id = state_service.record_run_start("p", "t")
        lesson_id = state_service.record_lesson(
            run_id=run_id,
            project="p",
            role="developer",
            task="t",
            text="Validated lesson survives semantic fallback.",
            score=None,
            confidence=None,
            category="general",
            validated=False,
        )
        state_service.update_lesson_validation(lesson_id, validated=True, score=0.8)

        settings.enable_semantic_lesson_retrieval = True
        try:
            lessons = retrieve_lessons("p", role="developer", task="t", limit=5, semantic=True)
        finally:
            settings.enable_semantic_lesson_retrieval = False

        assert [lesson.text for lesson in lessons] == [
            "Validated lesson survives semantic fallback."
        ]
