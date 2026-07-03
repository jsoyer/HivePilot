"""Tests for the bounded challenge-rebuttal flow (Part B).

Covers:
- _resolve_role_from_display: display name → role key resolution
- _run_rebuttal_round: triggers 🛡️ then ⚖️/🙋 turns (mocked capture_definition)
- Config gates: max_challenge_rounds=0 / enable_challenge_rounds=False → no rebuttal
- Escalation (MAINTAIN) surfaces NEEDS_HUMAN note in prior_chunks
- simulate=True → no real capture_definition calls
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import PipelineConfig, PipelineStage, ProjectConfig, TaskConfig, TaskStep
from hivepilot.orchestrator import _resolve_role_from_display
from hivepilot.services import notification_service as ns

# ---------------------------------------------------------------------------
# Helpers — mirrors test_orchestrator.py
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


# ---------------------------------------------------------------------------
# 1. _resolve_role_from_display: display name → role key
# ---------------------------------------------------------------------------


class TestResolveRoleFromDisplay:
    def test_full_display_name_with_title(self) -> None:
        """'Aliénor (CEO)' resolves to 'ceo'."""
        result = _resolve_role_from_display("Aliénor (CEO)")
        assert result == "ceo"

    def test_display_name_only(self) -> None:
        """'Jules' alone resolves to 'chief_of_staff'."""
        result = _resolve_role_from_display("Jules")
        assert result == "chief_of_staff"

    def test_title_only(self) -> None:
        """'CTO' alone resolves to 'cto'."""
        result = _resolve_role_from_display("CTO")
        assert result == "cto"

    def test_case_insensitive(self) -> None:
        """Match is case-insensitive."""
        result = _resolve_role_from_display("alienor (ceo)")
        assert result == "ceo"

    def test_unknown_returns_none(self) -> None:
        """Unrecognised display name returns None."""
        result = _resolve_role_from_display("Unknown Agent XYZ")
        assert result is None

    def test_partial_match_title(self) -> None:
        """'ciso' (partial / title) resolves correctly."""
        result = _resolve_role_from_display("ciso")
        assert result == "ciso"


# ---------------------------------------------------------------------------
# 2. Rebuttal flow: 🛡️ → ⚖️ (ACCEPT path)
# ---------------------------------------------------------------------------


class TestRebuttalFlowAccept:
    """When target replies ACCEPT, ⚖️ stream_resolved is called."""

    def test_rebuttal_accept_emits_shield_then_scales(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rebuttal_calls: list[tuple[str, str, str]] = []
        resolved_calls: list[tuple[str, str, str]] = []
        needs_human_calls: list[tuple[str, str, str]] = []

        monkeypatch.setattr(
            ns,
            "stream_rebuttal",
            lambda actor, target, point: rebuttal_calls.append((actor, target, point)),
        )
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

        orch = _make_orchestrator()
        orch.registry = MagicMock()

        # First call = target rebuttal, second call = challenger resolution
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My analysis is correct because X.",
            "ACCEPT: Their defence is convincing.",
        ]

        # Set up tasks: plan-task (upstream, CEO role) + review-task (challenger, reviewer role)
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

        # Set up projects
        project = ProjectConfig(path=Path("/tmp/test-project"))
        orch.projects = MagicMock()
        orch.projects.projects = {"test-project": project}

        # Build stages
        upstream = PipelineStage(name="planning", task="plan-task")
        challenger_stage = PipelineStage(name="review", task="review-task")
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nSome CEO output here."]

        with (
            patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-5")),
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

        # 🛡️ must be emitted
        assert len(rebuttal_calls) == 1
        actor, target, point = rebuttal_calls[0]
        assert "CEO" in actor or "Aliénor" in actor
        assert "Victor" in target or "Reviewer" in target

        # ⚖️ must be emitted (ACCEPT path)
        assert len(resolved_calls) == 1
        assert len(needs_human_calls) == 0

        # Resolution note appended to prior_chunks
        assert any("[RESOLVED]" in chunk for chunk in prior_chunks)
        # No NEEDS_HUMAN note
        assert not any("[NEEDS_HUMAN]" in chunk for chunk in prior_chunks)


# ---------------------------------------------------------------------------
# 3. Rebuttal flow: 🛡️ → 🙋 (MAINTAIN / escalation path)
# ---------------------------------------------------------------------------


class TestRebuttalFlowEscalation:
    """When challenger replies MAINTAIN, 🙋 stream_needs_human is called."""

    def test_rebuttal_maintain_emits_needs_human(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rebuttal_calls: list[tuple] = []
        resolved_calls: list[tuple] = []
        needs_human_calls: list[tuple] = []

        monkeypatch.setattr(
            ns,
            "stream_rebuttal",
            lambda actor, target, point: rebuttal_calls.append((actor, target, point)),
        )
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

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My numbers are correct.",
            "MAINTAIN: I still disagree — needs escalation.",
        ]

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
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with (
            patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-5")),
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
        ):
            orch._run_rebuttal_round(
                challenger_name="Victor (Reviewer)",
                challenge_target="Aliénor (CEO)",
                challenge_point="The roadmap is wrong.",
                challenger_stage=challenger_stage,
                completed_stages=[upstream],
                prior_chunks=prior_chunks,
                policy=None,
                project_name="test-project",
                simulate=False,
            )

        # 🛡️ emitted
        assert len(rebuttal_calls) == 1
        # 🙋 emitted (MAINTAIN path)
        assert len(needs_human_calls) == 1
        assert len(resolved_calls) == 0

        # NEEDS_HUMAN note in prior_chunks
        assert any("[NEEDS_HUMAN]" in chunk for chunk in prior_chunks)
        assert not any("[RESOLVED]" in chunk for chunk in prior_chunks)


# ---------------------------------------------------------------------------
# 4. Config gates: max_challenge_rounds=0 → no rebuttal
# ---------------------------------------------------------------------------


class TestConfigGates:
    def _run_pipeline_with_challenge(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Run a 2-stage pipeline where stage 2 issues a challenge; return stream icon sequence."""
        icons_emitted: list[str] = []

        def fake_stream_agent_turn(actor, stage=None, target=None, summary=None, icon=None):
            if icon:
                icons_emitted.append(icon)

        monkeypatch.setattr(ns, "stream_agent_turn", fake_stream_agent_turn)
        monkeypatch.setattr(ns, "stream_challenge", lambda **kw: icons_emitted.append("⚔️"))
        monkeypatch.setattr(ns, "stream_rebuttal", lambda **kw: icons_emitted.append("🛡️"))
        monkeypatch.setattr(ns, "stream_resolved", lambda **kw: icons_emitted.append("⚖️"))
        monkeypatch.setattr(ns, "stream_needs_human", lambda **kw: icons_emitted.append("🙋"))

        return icons_emitted

    def test_max_challenge_rounds_zero_no_rebuttal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_challenge_rounds=0 → only ⚔️, no 🛡️."""
        import hivepilot.orchestrator as _orch_mod

        icons: list[str] = []
        monkeypatch.setattr(ns, "stream_agent_turn", lambda **kw: None)
        monkeypatch.setattr(ns, "emit_event", lambda *a, **kw: None)
        monkeypatch.setattr(ns, "stream_challenge", lambda **kw: icons.append("⚔️"))
        monkeypatch.setattr(ns, "stream_rebuttal", lambda **kw: icons.append("🛡️"))
        # Disable rebuttal via max_challenge_rounds=0 on the settings singleton
        monkeypatch.setattr(_orch_mod.settings, "max_challenge_rounds", 0, raising=False)
        monkeypatch.setattr(_orch_mod.settings, "enable_challenge_rounds", True, raising=False)

        orch = _make_orchestrator()
        orch.registry = MagicMock()

        project = ProjectConfig(path=Path("/tmp/p"))
        orch.projects = MagicMock()
        orch.projects.projects = {"p": project}

        # Wire tasks so validate_pipeline (called at runtime) can resolve stages
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

        challenge_stage_detail = (
            "status: PASS\nsummary:\n- reviewed.\n"
            "challenge: Aliénor (CEO) -- roadmap is unrealistic\n"
        )
        call_count = [0]

        def side_effect_run_task(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [
                    MagicMock(
                        project="p", success=True, detail="status: PASS\nsummary:\n- CEO planned."
                    )
                ]
            return [MagicMock(project="p", success=True, detail=challenge_stage_detail)]

        with (
            patch.object(orch, "run_task", side_effect=side_effect_run_task),
            patch.object(_orch_mod, "state_service") as mock_state,
            patch.object(_orch_mod, "policy_service") as mock_policy,
            patch.object(_orch_mod, "write_stage_artifact"),
            patch.object(_orch_mod, "InteractionService", return_value=MagicMock()),
            patch.object(_orch_mod, "validate_pipeline", return_value=None),
        ):
            mock_state.record_run_start.return_value = 1
            mock_state.complete_run.return_value = None
            mock_policy.get_policy.return_value = None
            orch.run_pipeline(
                project_names=["p"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                simulate=False,
            )

        # max_challenge_rounds=0 means no rebuttal even when ⚔️ fires
        assert "🛡️" not in icons

    def test_enable_challenge_rounds_false_no_rebuttal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enable_challenge_rounds=False → no 🛡️ emitted."""
        import hivepilot.orchestrator as _orch_mod

        icons: list[str] = []
        monkeypatch.setattr(ns, "stream_agent_turn", lambda **kw: None)
        monkeypatch.setattr(ns, "emit_event", lambda *a, **kw: None)
        monkeypatch.setattr(ns, "stream_challenge", lambda **kw: icons.append("⚔️"))
        monkeypatch.setattr(ns, "stream_rebuttal", lambda **kw: icons.append("🛡️"))
        # Disable rebuttal via the settings singleton
        monkeypatch.setattr(_orch_mod.settings, "enable_challenge_rounds", False, raising=False)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        project = ProjectConfig(path=Path("/tmp/p"))
        orch.projects = MagicMock()
        orch.projects.projects = {"p": project}

        # Wire tasks so validate_pipeline (called at runtime) can resolve stages
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

        challenge_stage_detail = (
            "status: PASS\nsummary:\n- reviewed.\n"
            "challenge: Aliénor (CEO) -- roadmap is unrealistic\n"
        )
        call_count = [0]

        def side_effect_run_task(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [
                    MagicMock(
                        project="p", success=True, detail="status: PASS\nsummary:\n- CEO planned."
                    )
                ]
            return [MagicMock(project="p", success=True, detail=challenge_stage_detail)]

        with (
            patch.object(orch, "run_task", side_effect=side_effect_run_task),
            patch.object(_orch_mod, "state_service") as mock_state,
            patch.object(_orch_mod, "policy_service") as mock_policy,
            patch.object(_orch_mod, "write_stage_artifact"),
            patch.object(_orch_mod, "InteractionService", return_value=MagicMock()),
            patch.object(_orch_mod, "validate_pipeline", return_value=None),
        ):
            mock_state.record_run_start.return_value = 1
            mock_state.complete_run.return_value = None
            mock_policy.get_policy.return_value = None
            orch.run_pipeline(
                project_names=["p"],
                pipeline_name="test-pipe",
                extra_prompt=None,
                auto_git=False,
                simulate=False,
            )

        assert "🛡️" not in icons


# ---------------------------------------------------------------------------
# 5. Escalation surfaces NEEDS_HUMAN in prior_chunks context
# ---------------------------------------------------------------------------


class TestEscalationContext:
    def test_needs_human_note_in_prior_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NEEDS_HUMAN note is written into prior_chunks when challenger maintains."""
        monkeypatch.setattr(ns, "stream_rebuttal", lambda **kw: None)
        monkeypatch.setattr(ns, "stream_resolved", lambda **kw: None)
        monkeypatch.setattr(ns, "stream_needs_human", lambda **kw: None)

        orch = _make_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.side_effect = [
            "DEFEND: My numbers stand.",
            "MAINTAIN: Still disagree.",
        ]

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
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with (
            patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-5")),
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
        ):
            orch._run_rebuttal_round(
                challenger_name="Victor (Reviewer)",
                challenge_target="Aliénor (CEO)",
                challenge_point="The plan is wrong.",
                challenger_stage=challenger_stage,
                completed_stages=[upstream],
                prior_chunks=prior_chunks,
                policy=None,
                project_name="test-project",
                simulate=False,
            )

        # The resolution note must be in prior_chunks and contain NEEDS_HUMAN
        resolution_chunks = [c for c in prior_chunks if "Challenge Resolution" in c]
        assert len(resolution_chunks) == 1
        assert "NEEDS_HUMAN" in resolution_chunks[0]
        assert (
            "Victor (Reviewer)" in resolution_chunks[0]
            or "challenge" in resolution_chunks[0].lower()
        )


# ---------------------------------------------------------------------------
# 6. simulate=True: no real capture_definition calls
# ---------------------------------------------------------------------------


class TestSimulateMode:
    def test_simulate_skips_capture_definition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In simulate mode, capture_definition is never called for rebuttal."""
        monkeypatch.setattr(ns, "stream_rebuttal", lambda **kw: None)
        monkeypatch.setattr(ns, "stream_resolved", lambda **kw: None)
        monkeypatch.setattr(ns, "stream_needs_human", lambda **kw: None)

        orch = _make_orchestrator()
        orch.registry = MagicMock()

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
        prior_chunks: list[str] = ["## Aliénor (CEO) (planning)\nCEO output."]

        with (
            patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-5")),
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
        ):
            orch._run_rebuttal_round(
                challenger_name="Victor (Reviewer)",
                challenge_target="Aliénor (CEO)",
                challenge_point="Challenge point.",
                challenger_stage=challenger_stage,
                completed_stages=[upstream],
                prior_chunks=prior_chunks,
                policy=None,
                project_name="test-project",
                simulate=True,
            )

        # No real API calls made
        orch.registry.capture_definition.assert_not_called()

        # Resolution note is still appended (simulated paths produce output)
        assert any("Challenge Resolution" in c for c in prior_chunks)
