"""Tests for Tier-2 on-demand orchestrator-mediated agent-to-agent requests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from hivepilot.services import notification_service as ns
from hivepilot.services import state_service
from hivepilot.services.agent_report import parse_agent_requests
from hivepilot.services.interaction_service import log_request_interaction

# ---------------------------------------------------------------------------
# C2: parse_agent_requests — pure function
# ---------------------------------------------------------------------------


class TestParseAgentRequests:
    def test_parses_single_request_emdash(self):
        text = "request: CTO — What is the current database schema version?"
        result = parse_agent_requests(text)
        assert result == [("CTO", "What is the current database schema version?")]

    def test_parses_single_request_double_dash(self):
        text = "request: CISO -- Are there open CVEs in our dependencies?"
        result = parse_agent_requests(text)
        assert result == [("CISO", "Are there open CVEs in our dependencies?")]

    def test_parses_uppercase_REQUEST(self):
        text = "REQUEST: Developer — Which API endpoints are affected?"
        result = parse_agent_requests(text)
        assert result == [("Developer", "Which API endpoints are affected?")]

    def test_ignores_none_value(self):
        text = "request: none"
        result = parse_agent_requests(text)
        assert result == []

    def test_ignores_malformed_no_separator(self):
        text = "request: some question without target"
        result = parse_agent_requests(text)
        assert result == []

    def test_ignores_empty_question(self):
        text = "request: CTO — "
        result = parse_agent_requests(text)
        assert result == []

    def test_parses_multiple_requests(self):
        text = (
            "request: CTO — What model is used for code generation?\n"
            "request: CISO — Is the API token rotated quarterly?\n"
        )
        result = parse_agent_requests(text)
        assert len(result) == 2
        assert result[0] == ("CTO", "What model is used for code generation?")
        assert result[1] == ("CISO", "Is the API token rotated quarterly?")

    def test_ignores_non_request_lines(self):
        text = (
            "status: PASS\n"
            "summary: All good\n"
            "challenge: CTO — timeline too aggressive\n"
            "request: CISO — Any open vulnerabilities?\n"
        )
        result = parse_agent_requests(text)
        assert result == [("CISO", "Any open vulnerabilities?")]

    def test_empty_text(self):
        assert parse_agent_requests("") == []


# ---------------------------------------------------------------------------
# C4: stream_agent_request / stream_agent_answer — notification streaming
# ---------------------------------------------------------------------------


class TestStreamAgentRequest:
    def test_stream_agent_request_emits_question_turn(self, monkeypatch):
        monkeypatch.setattr(ns.settings, "telegram_stream_live", True)
        monkeypatch.setattr(ns.settings, "telegram_stream_rich", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", "test_chat")
        captured = []

        def fake_send(message, **kwargs):
            captured.append(message)

        monkeypatch.setattr(ns, "_send_telegram", fake_send)
        ns.stream_agent_request(
            requester="CTO", target="CISO", question="Are dependencies patched?"
        )
        assert len(captured) == 1
        assert "❓" in captured[0]

    def test_stream_agent_answer_emits_answer_turn(self, monkeypatch):
        monkeypatch.setattr(ns.settings, "telegram_stream_live", True)
        monkeypatch.setattr(ns.settings, "telegram_stream_rich", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", "test_chat")
        captured = []

        def fake_send(message, **kwargs):
            captured.append(message)

        monkeypatch.setattr(ns, "_send_telegram", fake_send)
        ns.stream_agent_answer(target="CISO", requester="CTO", answer_excerpt="Yes, all patched.")
        assert len(captured) == 1
        assert "↩️" in captured[0]


# ---------------------------------------------------------------------------
# C5d: log_request_interaction
# ---------------------------------------------------------------------------


class TestLogRequestInteraction:
    def test_log_request_records_action(self):
        recorded = []

        def fake_record(**kwargs):
            recorded.append(dict(kwargs))
            return 0

        with patch.object(state_service, "record_interaction", side_effect=fake_record):
            log_request_interaction(actor="CTO", target="CISO", question="Open CVEs?")
        assert recorded[0]["action"] == "request"
        assert recorded[0]["actor"] == "CTO"

    def test_log_answer_records_answer_action(self):
        recorded = []

        def fake_record(**kwargs):
            recorded.append(dict(kwargs))
            return 0

        with patch.object(state_service, "record_interaction", side_effect=fake_record):
            log_request_interaction(
                actor="CISO", target="CTO", question="[ANSWER] No open CVEs found."
            )
        assert recorded[0]["action"] == "answer"


# ---------------------------------------------------------------------------
# Regression: _handle_agent_requests must reach the runner with a resolved,
# non-empty prompt_file, and (since the synthetic-project fix) a valid
# synthetic `ProjectConfig` rather than `project=None` — a question/answer
# exchange still isn't a coding task against a real repo, but every runner
# unconditionally dereferences `payload.project.*` (env/secrets/path/...),
# so `None` crashed with `AttributeError: 'NoneType' object has no attribute
# '<whichever field that runner touched first>'` — the exact bug class this
# fix eliminates. Same bug class as the human_challenge()/CoS live bug
# ("requires a prompt_file for Claude runner").
# ---------------------------------------------------------------------------


def _make_orchestrator_with_pipeline():
    from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage
    from hivepilot.orchestrator import Orchestrator

    pipeline = PipelineConfig(description="test", stages=[PipelineStage(name="dev", task="dev")])
    pipelines_file = PipelinesFile(pipelines={"test-pipe": pipeline})

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
        patch("hivepilot.orchestrator.validate_pipeline", return_value=None),
    ):
        return Orchestrator()


class TestHandleAgentRequestsPromptFile:
    def test_request_reaches_runner_with_resolved_prompt_file(self):
        orch = _make_orchestrator_with_pipeline()
        orch.registry = MagicMock()
        orch.registry.capture_definition = MagicMock(return_value="Kimi K2.7.")

        with (
            patch("hivepilot.orchestrator.notification_service"),
            patch("hivepilot.orchestrator.log_request_interaction"),
        ):
            result = orch._handle_agent_requests(
                stage_output="request: CTO — What model runs code generation?",
                actor="Developer",
                stage=MagicMock(),
                project_names=["myproject"],
                policy=None,
                budget={"remaining": 5},
            )

        assert "[ANSWER from CTO]: Kimi K2.7." in result
        payload = orch.registry.capture_definition.call_args[0][1]
        # `project=None` used to be passed here -- this asserts the fix: a
        # valid, obviously-synthetic ProjectConfig instead (see
        # `orchestrator._synthetic_project`), never `None`.
        assert payload.project is not None
        assert payload.project.env == {}
        assert payload.project.secrets == {}
        assert "agent-request" in (payload.project.description or "")
        assert payload.step.prompt_file, "prompt_file must not be empty"
        assert Path(payload.step.prompt_file).exists()


# ---------------------------------------------------------------------------
# End-to-end: drive the WHOLE agent-request flow through a REAL RunnerRegistry
# + REAL ClaudeRunner (only `subprocess.run` mocked) so the actual
# `_build_invocation`/`_assemble_prompt`/env-merge code paths that crashed in
# production (`'NoneType' object has no attribute 'env'`, then `.path`, then
# the missing prompt_file) are genuinely exercised — not a stubbed runner.
# ---------------------------------------------------------------------------


class TestHandleAgentRequestsEndToEnd:
    def test_request_end_to_end_real_claude_runner_no_crash(self, tmp_path):
        from hivepilot.registry import RunnerRegistry
        from hivepilot.roles import Role

        prompt_file = tmp_path / "developer.md"
        prompt_file.write_text("You are the developer agent.\n", encoding="utf-8")
        fake_role = Role(
            name="developer",
            title="Developer",
            prompt_file=prompt_file,
            model_profile="coding",
            inputs=[],
            outputs=[],
            can_block=False,
            order=1,
            runner="claude",
            model="test-model",
        )

        orch = _make_orchestrator_with_pipeline()
        orch.registry = RunnerRegistry(runner_defs={})

        with (
            patch.dict("hivepilot.roles.ROLES", {"developer": fake_role}, clear=True),
            patch("hivepilot.orchestrator.notification_service"),
            patch("hivepilot.orchestrator.log_request_interaction"),
            patch("hivepilot.runners.claude_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                stdout="Claude handles code generation.", returncode=0
            )
            result = orch._handle_agent_requests(
                stage_output="request: Developer — What model runs code generation?",
                actor="CTO",
                stage=MagicMock(),
                project_names=["myproject"],
                policy=None,
                budget={"remaining": 5},
            )

        assert "[ANSWER from Developer]: Claude handles code generation." in result
        assert mock_run.called, "the real ClaudeRunner must reach subprocess.run"
        _, run_kwargs = mock_run.call_args
        # cwd came from the synthetic project's `.path` -- proves
        # `payload.project.path` was accessed without crashing.
        assert run_kwargs["cwd"] == str(Path(tempfile.gettempdir()))
