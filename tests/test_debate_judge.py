"""
Tests for Sprint 1 (Debate Judge & Consensus PRD) — the flag-gated LLM
synthesis judge.

Covers:
- `enable_debate_judge` defaults False and the flags-off path is byte-identical
  to pre-judge behaviour (templated decision, majority path untouched).
- `enable_debate_judge=True` drives the ADR decision/confidence from ONE mocked
  judge `capture_definition` call.
- A malformed/empty judge response falls back to the templated decision —
  NEVER fabricates a decision.
- The judge call reuses `_resolve_secrets` and its output is masked before it
  reaches the ADR (same masking guarantee as the rest of the run).
- `DebateService` stays pure (no runner/subprocess/HTTP imports).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.orchestrator  # noqa: F401 — side-effect import for patch resolution
from hivepilot.config import settings
from hivepilot.models import PipelineConfig, PipelineStage
from hivepilot.services import config_provenance

MARKER = "JUDGE-SECRET-MARKER-9c1a7f3e-DO-NOT-LEAK"

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_orchestrator.py)
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


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestJudgeConfigDefaults:
    def test_judge_flags_default_off(self) -> None:
        from hivepilot.config import Settings

        s = Settings()
        assert s.enable_debate_judge is False
        assert s.judge_runner == "claude"
        assert s.judge_model is None


# ---------------------------------------------------------------------------
# Flags-off byte-identical path
# ---------------------------------------------------------------------------


class TestFlagOffByteIdentical:
    def test_flag_off_uses_templated_decision_and_no_judge_call(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        assert settings.enable_debate_judge is False  # sprint invariant

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="p", role_name="ceo", topic="adopt X?", simulate=True
            )

        assert adr == {"path": "ADR.md", "dry_run": True}
        assert _FakeDebate.captured["decision"].startswith("Synthesis of 2 model proposals")
        assert _FakeDebate.captured["confidence"] is None
        # simulate=True + judge off => zero real runner calls of any kind
        orch.registry.capture_definition.assert_not_called()

    def test_flag_off_real_brains_make_exactly_two_capture_calls(self, monkeypatch) -> None:
        """With real brains (simulate=False) and the judge OFF, there must be
        exactly 2 `capture_definition` calls (one per brain) and NO 3rd judge
        call — the flag-off path spawns no judge runner."""
        from hivepilot.models import ProjectConfig

        assert settings.enable_debate_judge is False  # sprint invariant

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)

        orch.registry.capture_definition.side_effect = ["brain one output", "brain two output"]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=False)

        # exactly 2 brain calls, NO judge call
        assert orch.registry.capture_definition.call_count == 2
        assert _FakeDebate.captured["decision"].startswith("Synthesis of 2 model proposals")
        assert _FakeDebate.captured["confidence"] is None


# ---------------------------------------------------------------------------
# Judge enabled — real synthesis (mocked capture_definition)
# ---------------------------------------------------------------------------


class TestJudgeEnabled:
    def test_judge_decision_and_confidence_drive_the_adr(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)
        monkeypatch.setattr(settings, "enable_debate_judge", True)

        judge_json = (
            '{"decision": "Adopt plan X after weighing both proposals.", '
            '"confidence": 0.87, '
            '"per_role_stance": {"ceo:opencode-go/qwen3.7-max": "favor"}}'
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
        assert _FakeDebate.captured["decision"] == "Adopt plan X after weighing both proposals."
        assert _FakeDebate.captured["confidence"] == 0.87
        # exactly 2 brain calls + 1 judge call
        assert orch.registry.capture_definition.call_count == 3

    def test_malformed_judge_response_falls_back_to_templated_decision(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)
        monkeypatch.setattr(settings, "enable_debate_judge", True)

        orch.registry.capture_definition.side_effect = [
            "brain one output",
            "brain two output",
            "not valid json at all",
        ]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=False)

        assert _FakeDebate.captured["decision"].startswith("Synthesis of 2 model proposals")
        assert _FakeDebate.captured["confidence"] is None

    def test_empty_judge_response_never_fabricates_a_decision(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)
        monkeypatch.setattr(settings, "enable_debate_judge", True)

        orch.registry.capture_definition.side_effect = ["brain one", "brain two", ""]

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=False)

        assert _FakeDebate.captured["decision"].startswith("Synthesis of 2 model proposals")
        assert _FakeDebate.captured["confidence"] is None

    def test_judge_never_called_when_simulate(self, monkeypatch) -> None:
        """simulate=True must short-circuit the judge call too (no real capture)."""
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)
        monkeypatch.setattr(settings, "enable_debate_judge", True)

        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=True)

        orch.registry.capture_definition.assert_not_called()
        # A synthetic verdict is still produced deterministically (never a real call).
        assert _FakeDebate.captured["confidence"] is not None


# ---------------------------------------------------------------------------
# Secret masking on the judge path (Judge Reuses Secret Scope invariant)
# ---------------------------------------------------------------------------


class TestJudgeSecretMasking:
    def test_judge_output_is_redacted_before_reaching_the_adr(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        orch = _make_orchestrator_with_pipeline(_make_pipeline_by_name("x"))
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", _FakeDebate)
        monkeypatch.setattr(settings, "enable_debate_judge", True)

        def _resolve_secrets_stub(step, project=None, policy=None):
            # Mirrors the real `_resolve_secrets` contract: resolved values are
            # registered globally for masking before being handed to the runner.
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
            orch.run_debate(project_name="p", role_name="ceo", topic="adopt X?", simulate=False)

        assert MARKER not in _FakeDebate.captured["decision"]
        assert config_provenance.REDACTED in _FakeDebate.captured["decision"]


# ---------------------------------------------------------------------------
# DebateService stays pure (defense-in-depth alongside the grep invariant)
# ---------------------------------------------------------------------------


class TestDebateServiceStaysPure:
    def test_no_runner_subprocess_or_http_imports(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[1] / "hivepilot" / "services" / "debate_service.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        forbidden_substrings = ("runner", "subprocess", "httpx", "requests", "resolve_runner")
        imported_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.append(node.module)
        offenders = [
            name
            for name in imported_names
            if any(substr in name.lower() for substr in forbidden_substrings)
        ]
        assert offenders == [], f"DebateService must stay pure, found imports: {offenders}"


# ---------------------------------------------------------------------------
# DebateResult.confidence — optional field, backward compatible
# ---------------------------------------------------------------------------


class TestDebateResultConfidence:
    def test_confidence_defaults_to_none(self) -> None:
        from hivepilot.services.debate_service import DebateService, Position

        svc = DebateService(vault_path=None)
        positions = [Position(role="ceo", stance="adopt", rationale="fast")]
        result = svc.synthesize(topic="t", positions=positions, decision="adopt")
        assert result.confidence is None

    def test_confidence_threaded_through_synthesize_and_run(self, tmp_path: Path) -> None:
        from hivepilot.services.debate_service import DebateService, Position

        vault = tmp_path / "FakeVault"
        vault.mkdir()
        (vault / "03 - Decisions").mkdir()
        svc = DebateService(vault_path=vault, dry_run=True)
        positions = [Position(role="ceo", stance="adopt", rationale="fast")]
        emit = svc.run(topic="t", positions=positions, decision="adopt", confidence=0.75)
        assert emit is not None
        assert emit.get("confidence") == 0.75

    def test_majority_decision_path_still_intact_when_decision_is_none(self) -> None:
        from hivepilot.services.debate_service import DebateService, Position

        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="a"),
            Position(role="cto", stance="adopt", rationale="b"),
            Position(role="ciso", stance="reject", rationale="c"),
        ]
        result = svc.synthesize(topic="t", positions=positions)
        assert "adopt" in result.decision.lower()
        assert result.confidence is None


# ---------------------------------------------------------------------------
# _parse_verdict — direct unit tests of the fail-safe parse contract.
# This is the security-relevant core: a malformed/garbage judge response must
# NEVER become a confident (or fabricated) decision.
# ---------------------------------------------------------------------------


class TestParseVerdict:
    def test_valid_json_produces_decision_and_confidence(self) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict('{"decision": "Ship it", "confidence": 0.8}')
        assert v.decision == "Ship it"
        assert v.confidence == 0.8

    def test_fenced_json_block_is_stripped_and_parsed(self) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict('```json\n{"decision": "Ship it", "confidence": 0.5}\n```')
        assert v.decision == "Ship it"
        assert v.confidence == 0.5

    def test_per_role_stance_is_parsed_when_valid(self) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict(
            '{"decision": "d", "confidence": 0.5, "per_role_stance": {"ceo": "favor"}}'
        )
        assert v.per_role_stance == {"ceo": "favor"}

    def test_invalid_per_role_stance_is_dropped_not_fatal(self) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict('{"decision": "d", "confidence": 0.5, "per_role_stance": [1, 2]}')
        assert v.decision == "d"
        assert v.per_role_stance is None

    @pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
    def test_empty_or_whitespace_returns_no_decision(self, raw: str) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict(raw)
        assert v.decision is None
        assert v.confidence is None

    def test_non_json_garbage_returns_no_decision(self) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict("this is not json at all")
        assert v.decision is None
        assert v.confidence is None

    @pytest.mark.parametrize("raw", ["[1, 2]", '"hello"', "42"])
    def test_non_object_json_returns_no_decision(self, raw: str) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict(raw)
        assert v.decision is None
        assert v.confidence is None

    def test_missing_decision_key_returns_no_decision(self) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict('{"confidence": 0.9}')
        assert v.decision is None
        assert v.confidence is None

    @pytest.mark.parametrize("decision", [None, "", "   "])
    def test_null_or_empty_decision_returns_no_decision(self, decision) -> None:
        import json

        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict(json.dumps({"decision": decision, "confidence": 0.9}))
        assert v.decision is None
        assert v.confidence is None

    def test_missing_confidence_returns_no_decision(self) -> None:
        # Contract: confidence is REQUIRED — a decision without a numeric
        # confidence is not a confident decision, so the whole verdict is void.
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict('{"decision": "Ship it"}')
        assert v.decision is None
        assert v.confidence is None

    def test_bool_confidence_is_not_accepted(self) -> None:
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict('{"decision": "Ship it", "confidence": true}')
        assert v.decision is None
        assert v.confidence is None

    @pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_confidence_returns_no_decision(self, token: str) -> None:
        # json.loads accepts bare NaN/Infinity tokens by default; a non-finite
        # confidence must NOT be clamped into a (max) value — it is untrusted.
        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict('{"decision": "Ship it", "confidence": %s}' % token)
        assert v.decision is None
        assert v.confidence is None

    @pytest.mark.parametrize(("raw_conf", "expected"), [(1.5, 1.0), (-0.3, 0.0), (5, 1.0)])
    def test_out_of_range_confidence_is_clamped(self, raw_conf, expected: float) -> None:
        import json

        from hivepilot.orchestrator import _parse_verdict

        v = _parse_verdict(json.dumps({"decision": "Ship it", "confidence": raw_conf}))
        assert v.decision == "Ship it"
        assert v.confidence == expected
