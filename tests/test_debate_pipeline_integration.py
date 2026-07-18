"""End-to-end integration tests for the debate-judge-pipeline-yaml PRD
(Sprints 1+2 combined) — the FULL loop from a `debate:` YAML block through
`resolve_debate_config`, `validate_pipeline`, the real orchestrator judge/
arbiter call sites, and the fail-closed `perform_git_actions`/`is_blocking`
PR gate. Only the LLM boundary (`RunnerRegistry.capture_definition`) and the
`gh` CLI boundary (`git_service.promote_pr`/`merge_pr`) are mocked — every
resolver, validator, and gate function runs for real.

Covers the 5 scenarios required by Sprint 3:
(a) Two pipelines in one loaded config resolve independently — one with a
    `debate:` block (judge+arbiter+threshold all on), one without (floor
    default-off) — no cross-contamination between them.
(b) A non-approving verdict (MAINTAIN decision, or ACCEPT below the
    pipeline's own threshold) blocks `promote_pr` at the real gate.
(c) A global floor `enable_arbiter=True` survives a pipeline
    `debate: {enable_arbiter: false}` (strengthen-only OR) — the arbiter
    still runs and a non-ACCEPT arbiter verdict still reaches human
    escalation (NEEDS_HUMAN).
(d) A stage-level `confidence_threshold` overrides a looser pipeline-level
    one AT THE REAL GATE — proven both ways: the override blocks a verdict
    that the pipeline-only threshold would have passed.
(e) `confidence_threshold: 0` in a `debate:` block fails closed at
    `load_pipelines` (real YAML parse), both at pipeline level and stage
    level — never silently disables the gate.

Mocking style mirrors tests/test_debate_pipeline_threading.py (Sprint 2) and
tests/test_verdict_gate_failclosed.py (Sprint 3 gate) — reused verbatim
where practical.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import ValidationError

from hivepilot.config import settings
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
from hivepilot.orchestrator import Verdict
from hivepilot.services import config_provenance
from hivepilot.services import notification_service as ns
from hivepilot.services.git_service import ensure_repo  # noqa: F401 -- sanity import
from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines

# ---------------------------------------------------------------------------
# Fixtures — mirrors test_debate_pipeline_threading.py exactly, so global
# settings/secret-registry state never leaks between tests in this file (or
# into other test modules run in the same session).
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
# Helpers — mirrors tests/test_debate_pipeline_threading.py's helpers of the
# same name/shape.
# ---------------------------------------------------------------------------


class _FakeDebate:
    """Records the kwargs `DebateService.run` was called with."""

    captured: dict = {}

    def __init__(self, vault, dry_run=True) -> None:
        pass

    def run(self, topic, positions, decision=None, confidence=None, **kw):
        _FakeDebate.captured = {"decision": decision, "confidence": confidence}
        return {"path": "ADR.md", "dry_run": True}


def _make_orchestrator_with_pipelines(
    pipelines: dict[str, PipelineConfig], tasks: dict | None = None
):
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines=pipelines)

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch(
            "hivepilot.orchestrator.load_tasks",
            return_value=MagicMock(tasks=tasks or {}, runners={}),
        ),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        orch = Orchestrator()
    return orch


def _wire_stages(orch):
    """Mirrors test_challenge_arbiter.py / test_debate_pipeline_threading.py's
    `_wire_stages` — an upstream "planning" stage (CEO) and a "review" stage
    (Reviewer) that challenges it."""
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
    """Mirrors tests/test_pipeline_mode.py / test_debate_pipeline_threading.py's
    `_bare_orchestrator` — a real (empty) RunnerRegistry + stubbed plugins."""
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
    """Mirrors test_verdict_gate_failclosed.py's `_init_repo`."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    return ProjectConfig(path=tmp_path)


def _register_stub_runner(kind: str):
    """Registers a minimal capture-only runner under *kind* in the real,
    module-global RUNNER_MAP (mirrors test_debate_pipeline_threading.py's
    gate tests) so `_execute_task_body` can dispatch a real (non-role) step
    without touching any actual agent binary."""
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


# ---------------------------------------------------------------------------
# Scenario (a) — two pipelines, one config, independent resolution.
# ---------------------------------------------------------------------------


class TestTwoPipelinesOneConfigIsolatedResolution:
    def test_judged_pipeline_activates_while_plain_pipeline_stays_floor_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert settings.enable_debate_judge is False  # floor invariant

        judged_stage = PipelineStage(name="s", task="ceo-task")
        judged_pipeline = PipelineConfig(
            description="judged",
            stages=[judged_stage],
            debate=DebateConfig(enable_judge=True, enable_arbiter=True, confidence_threshold=0.7),
        )
        plain_stage = PipelineStage(name="s", task="ceo-task")
        plain_pipeline = PipelineConfig(description="plain", stages=[plain_stage])  # no debate:

        # Real (unmocked) validate_pipeline for BOTH pipelines against a
        # shared TasksFile -- proves they coexist in one config without
        # cross-validation errors.
        tasks = TasksFile(
            tasks={
                "ceo-task": TaskConfig(
                    description="d",
                    role="ceo",
                    engine="native",
                    steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
                )
            }
        )
        validate_pipeline(judged_pipeline, tasks)
        validate_pipeline(plain_pipeline, tasks)

        orch = _make_orchestrator_with_pipelines(
            {"judged-pipe": judged_pipeline, "plain-pipe": plain_pipeline}
        )
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        # -- judged pipeline: judge+arbiter activate despite floor OFF; the
        # pipeline's own 0.7 threshold resolves through, not the floor's --
        judged_effective = orch._effective_debate(judged_stage, judged_pipeline)
        assert judged_effective.enable_judge is True
        assert judged_effective.enable_arbiter is True
        assert judged_effective.confidence_threshold == 0.7

        judge_json = '{"decision": "Adopt via judged-pipe.", "confidence": 0.81}'
        orch.registry.capture_definition.side_effect = ["brain one", "brain two", judge_json]
        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="p",
                role_name="ceo",
                topic="adopt X?",
                simulate=False,
                debate_config=judged_effective,
            )
        assert adr == {"path": "ADR.md", "dry_run": True}
        assert orch.registry.capture_definition.call_count == 3  # 2 brains + 1 judge
        assert _FakeDebate.captured["confidence"] == 0.81

        # -- plain pipeline: no debate: block -> resolves exactly to the
        # floor (still off), completely unaffected by judged-pipe's block --
        plain_effective = orch._effective_debate(plain_stage, plain_pipeline)
        assert plain_effective.enable_judge is False
        assert plain_effective.enable_arbiter is False
        assert plain_effective.confidence_threshold == settings.judge_confidence_threshold

        orch.registry.capture_definition.reset_mock(side_effect=True)
        orch.registry.capture_definition.side_effect = ["brain one", "brain two"]
        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(
                project_name="p",
                role_name="ceo",
                topic="adopt X?",
                simulate=False,
                debate_config=plain_effective,
            )
        assert orch.registry.capture_definition.call_count == 2  # 2 brains only, NO judge call
        assert _FakeDebate.captured["confidence"] is None


# ---------------------------------------------------------------------------
# Scenario (b) — MAINTAIN / low-confidence under a pipeline threshold blocks
# promote_pr at the REAL gate (perform_git_actions + is_blocking, unmocked).
# ---------------------------------------------------------------------------


class TestGateBlocksOnMaintainOrLowConfidence:
    def test_maintain_decision_blocks_promote_pr_regardless_of_confidence(
        self, tmp_path: Path
    ) -> None:
        _register_stub_runner("gate-maintain-stub")
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner="gate-maintain-stub")],
                git=GitActions(promote_pr=True),
            )
            project = _init_repo(tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(enable_judge=True, confidence_threshold=0.5),
            )
            stage = PipelineStage(name="s", task="t")

            # Simulate a MAINTAIN verdict already registered earlier this run
            # (e.g. from a prior debate/challenge step) -- high confidence,
            # but MAINTAIN is not an approval decision, so it must still block.
            orch._governing_verdict = Verdict(decision="MAINTAIN", confidence=0.99)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
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

            mock_promote.assert_not_called()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop("gate-maintain-stub", None)

    def test_accept_below_pipeline_threshold_blocks_promote_pr(self, tmp_path: Path) -> None:
        _register_stub_runner("gate-lowconf-stub")
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner="gate-lowconf-stub")],
                git=GitActions(promote_pr=True),
            )
            project = _init_repo(tmp_path)
            pipeline = PipelineConfig(
                description="d",
                stages=[],
                debate=DebateConfig(enable_judge=True, confidence_threshold=0.8),
            )
            stage = PipelineStage(name="s", task="t")

            orch._governing_verdict = Verdict(decision="ACCEPT", confidence=0.4)

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
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

            mock_promote.assert_not_called()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop("gate-lowconf-stub", None)


# ---------------------------------------------------------------------------
# Scenario (c) — floor arbiter=True survives a pipeline `enable_arbiter:
# false` (strengthen-only) and still reaches human escalation.
# ---------------------------------------------------------------------------


class TestArbiterStrengthenOnlyReachesHumanEscalation:
    def test_floor_arbiter_survives_pipeline_disable_and_escalates_to_human(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)  # floor ON

        pipeline = PipelineConfig(
            description="test pipeline",
            stages=[
                PipelineStage(name="planning", task="plan-task"),
                PipelineStage(name="review", task="review-task"),
            ],
            debate=DebateConfig(enable_arbiter=False),  # pipeline TRIES to disable it
        )
        orch = _make_orchestrator_with_pipelines({"test-pipe": pipeline})
        orch.registry = MagicMock()
        # 1st call = target rebuttal, 2nd call = the INDEPENDENT arbiter judge
        # -- itself returning a non-ACCEPT decision, so the challenge must
        # escalate to human review even though the arbiter (correctly) ran.
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct and the roadmap is realistic.",
            '{"decision": "DEFEND", "confidence": 0.9}',
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks, pipeline)

        resolved_calls, needs_human_calls = _mock_streams
        # Exactly 2 calls (rebuttal + independent arbiter judge) -- if the
        # pipeline's `enable_arbiter: false` had (wrongly) won, the challenger
        # would self-adjudicate as a 2nd call of a DIFFERENT shape and never
        # reach the arbiter's ACCEPT/DEFEND vocabulary at all.
        assert orch.registry.capture_definition.call_count == 2
        assert len(resolved_calls) == 0
        assert len(needs_human_calls) == 1
        assert any("[NEEDS_HUMAN]" in c for c in prior_chunks)
        # "independent judge verdict" is text unique to the arbiter path's
        # resolution_output (see `_resolve_challenge_via_arbiter`) -- proves
        # the arbiter (not challenger self-adjudication) produced the escalation.
        assert any("independent judge verdict" in c for c in prior_chunks)


# ---------------------------------------------------------------------------
# Scenario (d) — stage-level confidence_threshold overrides pipeline-level
# AT THE REAL GATE, proven both directions with the SAME verdict confidence.
# ---------------------------------------------------------------------------


class TestStageThresholdOverridesPipelineAtTheGate:
    def test_stage_threshold_overrides_pipeline_and_blocks_at_the_gate(
        self, tmp_path: Path
    ) -> None:
        _register_stub_runner("gate-stage-override-stub")
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner="gate-stage-override-stub")],
                git=GitActions(promote_pr=True),
            )
            project = _init_repo(tmp_path)
            stage = PipelineStage(name="s", task="t", debate=DebateConfig(confidence_threshold=0.9))
            pipeline = PipelineConfig(
                description="d",
                stages=[stage],
                debate=DebateConfig(enable_judge=True, confidence_threshold=0.6),
            )

            # 0.75 is ABOVE the pipeline's 0.6 but BELOW the stage's 0.9 --
            # only the stage-override interpretation blocks this.
            orch._governing_verdict = Verdict(decision="ACCEPT", confidence=0.75)

            effective = orch._effective_debate(stage, pipeline)
            assert effective.confidence_threshold == 0.9  # stage wins the resolve

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
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

            mock_promote.assert_not_called()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop("gate-stage-override-stub", None)

    def test_same_confidence_passes_under_pipeline_only_threshold(self, tmp_path: Path) -> None:
        """Control for the test above: the IDENTICAL 0.75-confidence ACCEPT
        verdict, with NO stage-level override (pipeline's looser 0.6 governs
        alone), must PASS the gate -- proving the stage override in the
        sibling test is what actually flips the real outcome, not some
        unrelated difference in setup."""
        _register_stub_runner("gate-pipeline-only-stub")
        try:
            orch = _bare_orchestrator()
            task = TaskConfig(
                description="d",
                steps=[TaskStep(name="s", runner="gate-pipeline-only-stub")],
                git=GitActions(promote_pr=True),
            )
            project = _init_repo(tmp_path)
            stage = PipelineStage(name="s", task="t")  # no stage-level debate override
            pipeline = PipelineConfig(
                description="d",
                stages=[stage],
                debate=DebateConfig(enable_judge=True, confidence_threshold=0.6),
            )

            orch._governing_verdict = Verdict(decision="ACCEPT", confidence=0.75)

            effective = orch._effective_debate(stage, pipeline)
            assert effective.confidence_threshold == 0.6  # pipeline governs alone

            with (
                patch.object(orch, "_resolve_secrets", return_value={}),
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

            mock_promote.assert_called_once()
        finally:
            from hivepilot.registry import RUNNER_MAP

            RUNNER_MAP.pop("gate-pipeline-only-stub", None)


# ---------------------------------------------------------------------------
# Scenario (e) — confidence_threshold: 0 fails closed at REAL YAML load
# time (load_pipelines), both at pipeline level and stage level.
# ---------------------------------------------------------------------------


class TestConfidenceThresholdZeroFailsClosedAtLoad:
    def test_pipeline_level_zero_threshold_raises_on_yaml_load(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "pipelines.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "pipelines": {
                        "bad-pipe": {
                            "description": "d",
                            "stages": [{"name": "s", "task": "t"}],
                            "debate": {"confidence_threshold": 0},
                        }
                    }
                }
            )
        )
        with pytest.raises(ValidationError, match="confidence_threshold"):
            load_pipelines(path=yaml_path)

    def test_stage_level_zero_threshold_raises_on_yaml_load(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "pipelines.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "pipelines": {
                        "bad-pipe": {
                            "description": "d",
                            "stages": [
                                {
                                    "name": "s",
                                    "task": "t",
                                    "debate": {"confidence_threshold": 0},
                                }
                            ],
                        }
                    }
                }
            )
        )
        with pytest.raises(ValidationError, match="confidence_threshold"):
            load_pipelines(path=yaml_path)

    def test_absent_threshold_does_not_raise_and_never_disables_the_gate(
        self, tmp_path: Path
    ) -> None:
        """Control: a `debate:` block with NO confidence_threshold at all
        (absent, not zero) must load fine -- absence means "inherit the
        floor", never "no threshold" / "always pass"."""
        yaml_path = tmp_path / "pipelines.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "pipelines": {
                        "good-pipe": {
                            "description": "d",
                            "stages": [{"name": "s", "task": "t"}],
                            "debate": {"enable_judge": True},
                        }
                    }
                }
            )
        )
        pipelines_file = load_pipelines(path=yaml_path)
        pipeline = pipelines_file.pipelines["good-pipe"]
        assert pipeline.debate is not None
        assert pipeline.debate.confidence_threshold is None
        tasks = TasksFile(tasks={"t": TaskConfig(description="d")})
        validate_pipeline(pipeline, tasks)  # must not raise
