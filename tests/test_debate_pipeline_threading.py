"""Tests for Sprint 2 (debate-judge-pipeline-yaml PRD) — threading the
resolved pipeline/stage debate config into the 3 orchestrator call sites
(judge, arbiter, fail-closed gate) via ``Orchestrator._effective_debate``.

Covers:
- ``_effective_debate(None, None)`` is exactly the floor (sanity/regression
  lock for the fallback every call site uses when no pipeline/stage is in
  scope).
- Judge: a pipeline ``debate: {enable_judge: true}`` override activates the
  judge for that pipeline even while the global ``enable_debate_judge`` floor
  stays False; a pipeline WITHOUT a ``debate:`` block stays default-off. The
  ACTUAL ``run_debate``/``_run_debate_body`` call site is exercised (not just
  the resolver) to prove the resolved config actually drives the judge call.
- Arbiter: ``enable_challenge_arbiter`` is a floor OR — a pipeline
  ``debate: {enable_arbiter: false}`` can never turn OFF a floor ``True``
  (strengthen-only) — exercised via the real ``_run_rebuttal_round`` /
  ``_resolve_challenge_via_arbiter`` call chain.
- Gate: ``confidence_threshold`` resolves stage > pipeline > floor — a
  stage-level override wins over a looser pipeline-level default, and that
  resolved value is what reaches ``perform_git_actions``'s
  ``confidence_threshold`` kwarg via the real ``_execute_task_body`` call
  site (``is_blocking``/``git_service`` signatures untouched).
- Standalone ``run_debate`` (no ``stage``/``pipeline`` in scope, e.g. the CLI
  ``debate`` command) resolves to the floor only — unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.config import settings
from hivepilot.models import (
    DebateConfig,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    TaskConfig,
    TaskStep,
)
from hivepilot.services import config_provenance
from hivepilot.services import notification_service as ns

# ---------------------------------------------------------------------------
# Fixtures — guarantee floor flags/threshold and the secret registry never
# leak between tests (mirrors test_debate_judge.py / test_challenge_arbiter.py).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


@pytest.fixture(autouse=True)
def _reset_debate_settings() -> Iterator[None]:
    original_judge = settings.enable_debate_judge
    original_arbiter = settings.enable_challenge_arbiter
    original_threshold = settings.judge_confidence_threshold
    yield
    settings.enable_debate_judge = original_judge
    settings.enable_challenge_arbiter = original_arbiter
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
# Helpers — mirrors tests/test_debate_judge.py + tests/test_challenge_arbiter.py
# + tests/test_pipeline_mode.py
# ---------------------------------------------------------------------------


def _make_pipeline_by_name(*stage_names: str, debate: DebateConfig | None = None) -> PipelineConfig:
    stages = [PipelineStage(name=n, task=n) for n in stage_names]
    return PipelineConfig(description="test pipeline", stages=stages, debate=debate)


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
    """Records the kwargs `DebateService.run` was called with."""

    captured: dict = {}

    def __init__(self, vault, dry_run=True) -> None:
        pass

    def run(self, topic, positions, decision=None, confidence=None, **kw):
        _FakeDebate.captured = {
            "topic": topic,
            "positions": positions,
            "decision": decision,
            "confidence": confidence,
        }
        return {"path": "ADR.md", "dry_run": True}


def _wire_stages(orch) -> tuple[PipelineStage, PipelineStage]:
    """Mirrors test_challenge_arbiter.py's _wire_stages — an upstream
    "planning" stage (CEO) and a "review" stage (Reviewer) that challenges it."""
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


def _run_rebuttal(orch, upstream, challenger_stage, prior_chunks, pipeline) -> None:
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
            pipeline=pipeline,
        )


def _bare_orchestrator():
    """Mirrors tests/test_pipeline_mode.py's `_bare_orchestrator` — a real
    (empty) RunnerRegistry + stubbed plugins, so `_execute_task_body` resolves
    runner classes via RUNNER_MAP but performs no plugin/state side effects."""
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


# ---------------------------------------------------------------------------
# `_effective_debate` sanity/regression lock — the fallback every call site
# uses when it has no pipeline/stage of its own (`None, None`).
# ---------------------------------------------------------------------------


class TestEffectiveDebateFloorFallback:
    def test_none_none_equals_the_live_settings_floor(self) -> None:
        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        effective = orch._effective_debate(None, None)
        assert effective.enable_judge == settings.enable_debate_judge
        assert effective.enable_arbiter == settings.enable_challenge_arbiter
        assert effective.runner == settings.judge_runner
        assert effective.model == settings.judge_model
        assert effective.confidence_threshold == settings.judge_confidence_threshold


# ---------------------------------------------------------------------------
# Area 1 — Judge: pipeline `debate:` overrides the floor, per-pipeline.
# ---------------------------------------------------------------------------


class TestJudgeActivatesViaResolvedPipelineConfig:
    def test_pipeline_enable_judge_true_activates_judge_despite_floor_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prove the ACTUAL judge call site (`run_debate` / `_run_debate_body`)
        activates from a resolved `EffectiveDebateConfig` — exactly what
        `_execute_task_body` computes via `self._effective_debate(stage,
        pipeline)` and forwards as `debate_config` — even though the global
        `enable_debate_judge` floor stays False the whole test."""
        assert settings.enable_debate_judge is False  # floor invariant

        stage = PipelineStage(name="s", task="ceo-task")
        pipeline = PipelineConfig(
            description="d", stages=[stage], debate=DebateConfig(enable_judge=True)
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        judge_json = '{"decision": "Adopt via pipeline-scoped judge.", "confidence": 0.81}'
        orch.registry.capture_definition.side_effect = [
            "brain one output",
            "brain two output",
            judge_json,
        ]

        effective = orch._effective_debate(stage, pipeline)
        assert effective.enable_judge is True  # sanity: pipeline override resolved

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="p",
                role_name="ceo",
                topic="adopt X?",
                simulate=False,
                debate_config=effective,
            )

        assert adr == {"path": "ADR.md", "dry_run": True}
        assert _FakeDebate.captured["decision"] == "Adopt via pipeline-scoped judge."
        assert _FakeDebate.captured["confidence"] == 0.81
        # exactly 2 brain calls + 1 judge call -- the judge activated even
        # though settings.enable_debate_judge is still False.
        assert orch.registry.capture_definition.call_count == 3

    def test_pipeline_without_debate_block_stays_default_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pipeline with NO `debate:` block must resolve exactly like the
        floor (default off) -- byte-identical to pre-Sprint-2 behaviour."""
        assert settings.enable_debate_judge is False  # floor invariant

        stage = PipelineStage(name="s", task="ceo-task")
        pipeline = PipelineConfig(description="d", stages=[stage])  # no debate: block
        orch = _make_orchestrator_with_pipeline(pipeline)
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        orch.registry.capture_definition.side_effect = ["brain one output", "brain two output"]

        effective = orch._effective_debate(stage, pipeline)
        assert effective.enable_judge is False

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(
                project_name="p",
                role_name="ceo",
                topic="adopt X?",
                simulate=False,
                debate_config=effective,
            )

        # exactly 2 brain calls, NO judge call
        assert orch.registry.capture_definition.call_count == 2
        assert _FakeDebate.captured["decision"].startswith("Synthesis of 2 model proposals")
        assert _FakeDebate.captured["confidence"] is None


# ---------------------------------------------------------------------------
# Area 2 — Arbiter: strengthen-only OR across floor + pipeline + stage.
# ---------------------------------------------------------------------------


class TestArbiterStrengthenOnly:
    def test_pipeline_cannot_disable_a_floor_true_arbiter(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        """global enable_challenge_arbiter=True + pipeline debate:
        {enable_arbiter: false} -> the arbiter must STILL run: a pipeline/
        stage override can only ADD gating, never remove a floor `True`
        (empty-value/override-value-fail-open bug class)."""
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)  # floor ON

        pipeline = PipelineConfig(
            description="test pipeline",
            stages=[
                PipelineStage(name="planning", task="plan-task"),
                PipelineStage(name="review", task="review-task"),
            ],
            debate=DebateConfig(enable_arbiter=False),  # pipeline TRIES to disable it
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        orch.registry = MagicMock()
        # 1st call = target rebuttal, 2nd call = INDEPENDENT judge (arbiter
        # path) -- if the pipeline override had (wrongly) won, a 3rd
        # self-adjudication call from the challenger's OWN runner would follow.
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            '{"decision": "ACCEPT", "confidence": 0.9}',
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks, pipeline)

        resolved_calls, needs_human_calls = _mock_streams
        assert orch.registry.capture_definition.call_count == 2
        assert len(resolved_calls) == 1
        assert len(needs_human_calls) == 0
        # "independent judge verdict" is text unique to the arbiter path's
        # resolution_output (see `_resolve_challenge_via_arbiter`) -- proves
        # the arbiter (not challenger self-adjudication) produced the result.
        assert any("independent judge verdict" in c for c in prior_chunks)

    def test_stage_can_disable_when_floor_and_pipeline_are_both_off(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        """Baseline control: with the floor OFF and no pipeline override, the
        arbiter must NOT run (self-adjudication path) -- confirms the OR is
        genuinely gated, not always-on."""
        assert settings.enable_challenge_arbiter is False  # floor invariant

        pipeline = PipelineConfig(
            description="test pipeline",
            stages=[
                PipelineStage(name="planning", task="plan-task"),
                PipelineStage(name="review", task="review-task"),
            ],
        )
        orch = _make_orchestrator_with_pipeline(pipeline)
        orch.registry = MagicMock()
        # 1st call = target rebuttal, 2nd call = challenger's OWN resolution.
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            "ACCEPT: Their defence is convincing.",
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with patch.object(
            orch, "_adjudicate_challenge", side_effect=AssertionError("arbiter must not be used")
        ):
            _run_rebuttal(orch, upstream, challenger_stage, prior_chunks, pipeline)

        resolved_calls, needs_human_calls = _mock_streams
        assert orch.registry.capture_definition.call_count == 2
        assert len(resolved_calls) == 1
        assert len(needs_human_calls) == 0


# ---------------------------------------------------------------------------
# Area 3 — Gate: confidence_threshold resolves stage > pipeline > floor, and
# that resolved value is what reaches `perform_git_actions`.
# ---------------------------------------------------------------------------


class TestGateConfidenceThresholdStageOverPipeline:
    def test_stage_confidence_threshold_wins_over_pipeline_and_reaches_the_gate(
        self, tmp_path: Path
    ) -> None:
        from hivepilot.registry import RUNNER_MAP, RunnerRegistry
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

        RunnerRegistry.register("gate-threshold-stub", _StubRunner, override=True)
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d", steps=[TaskStep(name="s", runner="gate-threshold-stub")]
            )
            project = ProjectConfig(path=tmp_path)
            stage = PipelineStage(name="s", task="t", debate=DebateConfig(confidence_threshold=0.9))
            pipeline = PipelineConfig(
                description="d",
                stages=[stage],
                debate=DebateConfig(confidence_threshold=0.6),
            )

            captured: dict = {}

            def _capture_gate(**kwargs):
                captured.update(kwargs)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch("hivepilot.orchestrator.perform_git_actions", side_effect=_capture_gate),
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

            # 0.9 (stage) must win over 0.6 (pipeline) -- the ACTUAL value fed
            # into perform_git_actions's fail-closed gate.
            assert captured["confidence_threshold"] == 0.9
        finally:
            RUNNER_MAP.pop("gate-threshold-stub", None)

    def test_no_stage_no_pipeline_gate_uses_floor_threshold(self, tmp_path: Path) -> None:
        """A plain (non-pipeline) task run — `stage`/`pipeline` both default
        `None` — must feed the floor's `judge_confidence_threshold` into the
        gate, byte-identical to pre-Sprint-2 behaviour."""
        from hivepilot.registry import RUNNER_MAP, RunnerRegistry
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

        RunnerRegistry.register("gate-floor-stub", _StubRunner, override=True)
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(description="d", steps=[TaskStep(name="s", runner="gate-floor-stub")])
            project = ProjectConfig(path=tmp_path)

            captured: dict = {}

            def _capture_gate(**kwargs):
                captured.update(kwargs)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
                patch("hivepilot.orchestrator.perform_git_actions", side_effect=_capture_gate),
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
                    # no stage/pipeline kwargs -- plain task run
                )

            assert captured["confidence_threshold"] == settings.judge_confidence_threshold
            assert captured["judge_gate_enabled"] == (
                settings.enable_debate_judge or settings.enable_challenge_arbiter
            )
        finally:
            RUNNER_MAP.pop("gate-floor-stub", None)


# ---------------------------------------------------------------------------
# Standalone `run_debate` (no stage/pipeline) — floor behaviour, unchanged.
# ---------------------------------------------------------------------------


class TestStandaloneRunDebateUsesFloorOnly:
    def test_no_debate_config_kwarg_resolves_to_the_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Standalone `run_debate` (e.g. cli.py's `debate` command, which
        never passes `debate_config`) must resolve via the floor only."""
        monkeypatch.setattr(settings, "enable_debate_judge", True)
        monkeypatch.setattr(settings, "judge_confidence_threshold", 0.42)

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        judge_json = '{"decision": "Floor-driven decision.", "confidence": 0.9}'
        orch.registry.capture_definition.side_effect = [
            "brain one output",
            "brain two output",
            judge_json,
        ]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="p",
                role_name="ceo",
                topic="adopt X?",
                simulate=False,
                # no debate_config kwarg -- exactly cli.py's call shape
            )

        assert adr == {"path": "ADR.md", "dry_run": True}
        assert _FakeDebate.captured["decision"] == "Floor-driven decision."
        # exactly 2 brain calls + 1 judge call -- the judge activated purely
        # from the floor (settings.enable_debate_judge), with no debate_config.
        assert orch.registry.capture_definition.call_count == 3

    def test_no_debate_config_kwarg_floor_off_stays_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert settings.enable_debate_judge is False  # floor invariant

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        orch.registry.capture_definition.side_effect = ["brain one output", "brain two output"]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=False)

        assert orch.registry.capture_definition.call_count == 2
        assert _FakeDebate.captured["confidence"] is None
