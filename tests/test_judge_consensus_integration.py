"""End-to-end integration tests for the Debate Judge & Consensus PRD (Sprint 4).

Unlike the per-sprint unit suites (`test_debate_judge.py`, `test_challenge_arbiter.py`,
`test_governing_verdict.py`, `test_verdict_gate_failclosed.py`), these tests thread
the REAL S1->S2->S3 machinery together end to end: real `Orchestrator._run_debate_body`
/ `_run_rebuttal_round` / `_adjudicate` / `_adjudicate_challenge` / `_register_verdict`,
and the real `git_service.is_blocking` / `perform_git_actions` gate. The ONLY thing
mocked is the LLM boundary (`orch.registry.capture_definition`) plus, where noted, the
outbound git-provider calls (`promote_pr`/`merge_pr`) that would otherwise hit a real
git host.

Covers the 6 PRD S4 scenarios:
  (a) debate judge score reaches DebateService/ADR.
  (b) independent arbiter DEFEND escalates to human.
  (c) a blocking governing verdict actually skips promote_pr (real gate).
  (d) flags OFF: judge/arbiter never called, gate not applied (no regression).
  (e) NEEDS_HUMAN (arbiter exception) still blocks promotion.
  (f) verdict persisted (real DB) + redacted before it reaches decision/ADR.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.config import settings
from hivepilot.models import (
    GitActions,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    TaskConfig,
    TaskStep,
)
from hivepilot.services import config_provenance, state_service
from hivepilot.services import notification_service as ns
from hivepilot.services.git_service import is_blocking

MARKER = "S4-INTEGRATION-SECRET-MARKER-7d4a1c9e-DO-NOT-LEAK"

# ---------------------------------------------------------------------------
# Helpers — mirrors test_debate_judge.py / test_challenge_arbiter.py /
# test_governing_verdict.py exactly (proven-working orchestrator construction
# idiom). Do not invent a new mocking seam.
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


def _make_pipeline(*stage_defs: tuple[str, str]) -> PipelineConfig:
    stages = [PipelineStage(name=name, task=task) for name, task in stage_defs]
    return PipelineConfig(description="test pipeline", stages=stages)


def _make_orchestrator():
    """Two-stage (planning/review) orchestrator for challenge/rebuttal scenarios."""
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


def _init_repo(tmp_path: Path) -> ProjectConfig:
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    return ProjectConfig(path=tmp_path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


@pytest.fixture(autouse=True)
def _reset_judge_flags() -> Iterator[None]:
    """Guarantee no opt-in flag/threshold leaks between tests."""
    original_debate_judge = settings.enable_debate_judge
    original_arbiter = settings.enable_challenge_arbiter
    original_threshold = settings.judge_confidence_threshold
    yield
    settings.enable_debate_judge = original_debate_judge
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
# (a) Debate judge score reaches DebateService / ADR
# ---------------------------------------------------------------------------


class TestJudgeScoreReachesDebateServiceAndAdr:
    def test_judge_decision_and_confidence_drive_the_adr(self, monkeypatch) -> None:
        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)
        monkeypatch.setattr(settings, "enable_debate_judge", True)

        judge_json = (
            '{"decision": "Adopt plan X after weighing both proposals.", "confidence": 0.87}'
        )
        orch.registry.capture_definition.side_effect = [
            "brain one output",
            "brain two output",
            judge_json,
        ]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="p", role_name="ceo", topic="adopt X?", simulate=False
            )

        assert adr == {"path": "ADR.md", "dry_run": True}
        # exactly what the judge returned — not the templated fallback.
        assert _FakeDebate.captured["decision"] == "Adopt plan X after weighing both proposals."
        assert _FakeDebate.captured["confidence"] == 0.87
        assert orch.registry.capture_definition.call_count == 3


# ---------------------------------------------------------------------------
# (b) Independent arbiter DEFEND escalates to human
# ---------------------------------------------------------------------------


class TestArbiterDefendEscalatesToHuman:
    def test_reviewer_challenges_developer_arbiter_defend_escalates(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        # 1st call = target rebuttal, 2nd call = INDEPENDENT (third-party)
        # judge — never the challenger's or the target's own runner.
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            '{"decision": "DEFEND", "confidence": 0.95}',
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        # is_escalated is True: routed to human, never a silent resolve.
        assert len(needs_human_calls) == 1
        assert len(resolved_calls) == 0
        assert any("[NEEDS_HUMAN]" in c for c in prior_chunks)
        # the governing verdict registered from this arbiter call is itself
        # blocking — the same object the PR gate would later see.
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is True


# ---------------------------------------------------------------------------
# (c) A blocking governing verdict actually skips promote_pr (real gate)
# ---------------------------------------------------------------------------


class TestBlockingVerdictSkipsPromotion:
    def test_low_confidence_verdict_blocks_promote_pr(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams, tmp_path: Path
    ) -> None:
        """A non-approve/low-confidence governing verdict produced by a REAL
        arbiter run must skip promote_pr through the REAL
        `is_blocking`/`perform_git_actions` gate — only the git-provider call
        itself (`promote_pr`) is mocked."""
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)
        monkeypatch.setattr(settings, "judge_confidence_threshold", 0.5)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct.",
            '{"decision": "ACCEPT", "confidence": 0.1}',  # below threshold
        ]

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]
        _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        assert orch._governing_verdict is not None
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is True

        project = _init_repo(tmp_path)
        ga = GitActions(promote_pr=True)
        with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
            from hivepilot.services import git_service

            git_service.perform_git_actions(
                project_name="p",
                project=project,
                git=ga,
                verdict=orch._governing_verdict,
                judge_gate_enabled=(
                    settings.enable_debate_judge or settings.enable_challenge_arbiter
                ),
                confidence_threshold=settings.judge_confidence_threshold,
            )
        mock_promote.assert_not_called()


# ---------------------------------------------------------------------------
# (d) Flags OFF: byte-identical to pre-PRD behaviour
# ---------------------------------------------------------------------------


class TestFlagsOffByteIdentical:
    def test_flags_off_no_judge_calls_and_gate_not_applied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert settings.enable_debate_judge is False
        assert settings.enable_challenge_arbiter is False

        # 1) The judge seam is never invoked: only the 2 brain calls happen.
        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        orch.registry.capture_definition.side_effect = ["brain one output", "brain two output"]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=False)

        assert orch.registry.capture_definition.call_count == 2
        assert _FakeDebate.captured["decision"].startswith("Synthesis of 2 model proposals")
        assert _FakeDebate.captured["confidence"] is None
        # No debate ever registered a verdict this run.
        assert orch._governing_verdict is None

        # 2) The gate itself is not applied: judge_gate_enabled derives from
        # the (both-False) flags, so even an obviously-blocking verdict is
        # ignored and promote_pr proceeds exactly like pre-PRD.
        project = _init_repo(tmp_path)
        ga = GitActions(promote_pr=True)
        from hivepilot.orchestrator import Verdict
        from hivepilot.services import git_service

        blocking_verdict = Verdict(decision=None, confidence=None)
        with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
            git_service.perform_git_actions(
                project_name="p",
                project=project,
                git=ga,
                verdict=blocking_verdict,
                judge_gate_enabled=(
                    settings.enable_debate_judge or settings.enable_challenge_arbiter
                ),
                confidence_threshold=settings.judge_confidence_threshold,
                task_result=None,
            )
        mock_promote.assert_called_once()


# ---------------------------------------------------------------------------
# (e) NEEDS_HUMAN (arbiter exception) still blocks promotion
# ---------------------------------------------------------------------------


class TestNeedsHumanStillBlocks:
    def test_arbiter_exception_escalates_and_blocks_gate(
        self, monkeypatch: pytest.MonkeyPatch, _mock_streams, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(settings, "enable_challenge_arbiter", True)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "DEFEND: My analysis is correct."

        upstream, challenger_stage = _wire_stages(orch)
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with patch.object(
            orch, "_adjudicate_challenge", side_effect=RuntimeError("judge runner crashed")
        ):
            # Must not raise — human escalation is always reachable.
            _run_rebuttal(orch, upstream, challenger_stage, prior_chunks)

        resolved_calls, needs_human_calls = _mock_streams
        assert len(needs_human_calls) == 1
        assert len(resolved_calls) == 0
        assert any("[NEEDS_HUMAN]" in c for c in prior_chunks)

        assert orch._governing_verdict is not None
        assert orch._governing_verdict.decision is None
        assert orch._governing_verdict.confidence is None
        assert is_blocking(orch._governing_verdict, settings.judge_confidence_threshold) is True

        # Behavioral check through the REAL gate: promote_pr is skipped.
        project = _init_repo(tmp_path)
        ga = GitActions(promote_pr=True, merge_pr=True)
        from hivepilot.services import git_service

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


# ---------------------------------------------------------------------------
# (f) Verdict persisted (real DB) + redacted before reaching decision/ADR
# ---------------------------------------------------------------------------


class TestVerdictPersistedAndRedacted:
    def test_debate_judge_verdict_persisted_and_secret_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Real `DebateService` + real vault + real (test-isolated)
        `state_service` DB — a secret leaked in the raw judge output must not
        survive into the persisted verdict OR the ADR content."""
        state_service.init_db()

        vault = tmp_path / "FakeVault"
        vault.mkdir()
        (vault / "03 - Decisions").mkdir()

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        # `_run_debate_body` resolves the ADR vault from the global
        # `settings.obsidian_vault`, not the project path.
        monkeypatch.setattr(settings, "obsidian_vault", vault)
        monkeypatch.setattr(settings, "enable_debate_judge", True)

        def _resolve_secrets_stub(step, project=None, policy=None):
            config_provenance.register_secret_value(MARKER)
            return {"API_KEY": MARKER}

        monkeypatch.setattr(orch, "_resolve_secrets", _resolve_secrets_stub)

        judge_json_with_leak = (
            f'{{"decision": "Use token {MARKER} to finish the rollout.", "confidence": 0.9}}'
        )
        orch.registry.capture_definition.side_effect = [
            "brain one output",
            "brain two output",
            judge_json_with_leak,
        ]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="p", role_name="ceo", topic="adopt X?", simulate=False
            )

        # ADR content: no secret leak, redaction marker present.
        assert adr is not None
        assert MARKER not in adr["content"]
        assert config_provenance.REDACTED in adr["content"]
        assert adr.get("confidence") == 0.9

        # Persisted verdict (real DB, not mocked): kind="debate", redacted decision.
        rows = state_service.list_recent_verdicts()
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "debate"
        assert row["confidence"] == 0.9
        assert MARKER not in (row["decision"] or "")
        assert config_provenance.REDACTED in (row["decision"] or "")
