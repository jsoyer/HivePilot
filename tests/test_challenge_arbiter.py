"""Tests for Sprint 2 (Debate Judge & Consensus PRD) — the flag-gated
independent challenge arbiter.

Covers:
- `enable_challenge_arbiter` defaults False and the flags-off path is
  byte-identical to pre-Sprint-2 behaviour (challenger self-adjudicates via
  its OWN runner).
- `enable_challenge_arbiter=True` drives the resolution from ONE mocked
  INDEPENDENT judge `capture_definition` call — never re-invoking the
  challenger's own runner for the resolution check.
- The empty-value-fail-open bug class: any ambiguous/malformed/missing verdict
  field (None decision, None confidence, low confidence, non-ACCEPT decision,
  malformed/empty JSON, missing confidence, arbiter exception) escalates to
  `stream_needs_human` — never a silent ACCEPT.
- Secret masking: a leaked secret in the judge's raw output is redacted before
  it reaches `log_challenge_interaction` (Judge Reuses Secret Scope).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import PipelineConfig, PipelineStage, ProjectConfig, TaskConfig, TaskStep
from hivepilot.services import config_provenance
from hivepilot.services import notification_service as ns

MARKER = "ARBITER-SECRET-MARKER-4b2e9d71-DO-NOT-LEAK"

# ---------------------------------------------------------------------------
# Helpers — mirrors tests/test_challenge_rebuttal.py
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
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


@pytest.fixture(autouse=True)
def _reset_arbiter_flag() -> Iterator[None]:
    """Guarantee the opt-in flag never leaks between tests."""
    original = settings.enable_challenge_arbiter
    original_threshold = settings.judge_confidence_threshold
    yield
    settings.enable_challenge_arbiter = original
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
# Config defaults
# ---------------------------------------------------------------------------


class TestArbiterConfigDefaults:
    def test_arbiter_flags_default_off(self) -> None:
        from hivepilot.config import Settings

        s = Settings()
        assert s.enable_challenge_arbiter is False
        assert s.judge_confidence_threshold == 0.5


# ---------------------------------------------------------------------------
# Flag OFF — byte-identical self-adjudication path
# ---------------------------------------------------------------------------


class TestFlagOffSelfAdjudication:
    def test_flag_off_challenger_runner_invoked_arbiter_not(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        assert settings.enable_challenge_arbiter is False  # sprint invariant

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        # 1st call = target rebuttal, 2nd call = challenger's OWN resolution check
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            "ACCEPT: Their defence is convincing.",
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with patch.object(
            orch, "_adjudicate_challenge", side_effect=AssertionError("arbiter must not be used")
        ):
            _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        assert orch.registry.capture_definition.call_count == 2
        assert len(resolved_calls) == 1
        assert len(needs_human_calls) == 0
        assert any("[RESOLVED]" in c for c in prior_chunks)


# ---------------------------------------------------------------------------
# Flag ON — independent judge drives the resolution
# ---------------------------------------------------------------------------


class TestArbiterAccept:
    def test_confident_accept_resolves_without_challenger_reinvoke(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        # 1st call = target rebuttal, 2nd call = INDEPENDENT judge (never the
        # challenger's own runner for the resolution check).
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            '{"decision": "ACCEPT", "confidence": 0.9}',
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        # exactly 2 calls: rebuttal + judge — the challenger's own runner is
        # NEVER re-invoked for the resolution check under the arbiter path.
        assert orch.registry.capture_definition.call_count == 2
        assert len(resolved_calls) == 1
        assert len(needs_human_calls) == 0
        assert any("[RESOLVED]" in c for c in prior_chunks)


class TestArbiterEscalation:
    @pytest.mark.parametrize(
        "judge_raw",
        [
            '{"decision": "DEFEND", "confidence": 0.9}',
            '{"decision": "MAINTAIN", "confidence": 0.9}',
        ],
    )
    def test_non_accept_decision_escalates(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams, judge_raw: str
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            judge_raw,
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        assert len(needs_human_calls) == 1
        assert len(resolved_calls) == 0
        assert any("[NEEDS_HUMAN]" in c for c in prior_chunks)

    def test_low_confidence_accept_escalates(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)
        monkeypatch.setattr(settings, "judge_confidence_threshold", 0.5)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            '{"decision": "ACCEPT", "confidence": 0.1}',
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        assert len(needs_human_calls) == 1
        assert len(resolved_calls) == 0

    @pytest.mark.parametrize(
        "judge_raw",
        [
            "",
            "not valid json at all",
            "{}",
            '{"decision": "ACCEPT"}',  # missing confidence
            '{"confidence": 0.9}',  # missing decision
            '{"decision": null, "confidence": 0.9}',
        ],
    )
    def test_malformed_or_empty_verdict_escalates(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams, judge_raw: str
    ) -> None:
        """Empty-value-fail-open class: an absent/malformed/unparseable verdict
        or a missing confidence must NEVER be treated as a silent ACCEPT."""
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            judge_raw,
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        assert len(needs_human_calls) == 1, f"expected escalation for judge_raw={judge_raw!r}"
        assert len(resolved_calls) == 0

    def test_arbiter_exception_escalates_without_crash(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()

        def _raise_on_judge(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "DEFEND: My analysis is correct."
            raise RuntimeError("judge runner crashed")

        call_count = [0]
        orch.registry.capture_definition.side_effect = _raise_on_judge

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        # Must not raise — the pipeline continues with an escalation.
        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        assert len(needs_human_calls) == 1
        assert len(resolved_calls) == 0
        assert any("[NEEDS_HUMAN]" in c for c in prior_chunks)


# ---------------------------------------------------------------------------
# Secret masking (Judge Reuses Secret Scope invariant)
# ---------------------------------------------------------------------------


class TestJudgeArbiterSecretMasking:
    def test_judge_arbiter_masks_secret(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()

        def _resolve_secrets_stub(step, project=None, policy=None):
            config_provenance.register_secret_value(MARKER)
            return {"API_KEY": MARKER}

        monkeypatch.setattr(orch, "_resolve_secrets", _resolve_secrets_stub)

        judge_json_with_leak = f'{{"decision": "ACCEPT", "confidence": 0.9, "leak": "{MARKER}"}}'
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            judge_json_with_leak,
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        logged_points: list[str] = []
        p1, p2, p3, _p4 = _role_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "hivepilot.services.interaction_service.log_challenge_interaction",
                side_effect=lambda actor, target, point: logged_points.append(point),
            ),
        ):
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

        # The MARKER must never reach the persisted/logged resolution text —
        # `_adjudicate_challenge` masks via `redact_text` BEFORE `_parse_verdict`,
        # so it can't leak into decision/confidence, prior_chunks, or the log.
        assert not any(MARKER in p for p in logged_points)
        assert not any(MARKER in c for c in prior_chunks)
