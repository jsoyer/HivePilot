"""
Tests for hivepilot.services.debate_service.

All tests use tmp_path (pytest) — NEVER write to the real vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services.debate_service import DebateResult, DebateService, Position

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADR_FOLDER = "03 - Decisions"


def _make_adr_vault(tmp_path: Path) -> Path:
    """Create a minimal fake vault with the 03 - Decisions folder."""
    vault = tmp_path / "FakeVault"
    vault.mkdir()
    (vault / _ADR_FOLDER).mkdir()
    return vault


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------


class TestPosition:
    def test_position_is_immutable(self) -> None:
        p = Position(role="ceo", stance="adopt", rationale="cost savings")
        with pytest.raises(Exception):
            p.role = "cto"  # type: ignore[misc]

    def test_position_fields(self) -> None:
        p = Position(role="cto", stance="reject", rationale="too risky")
        assert p.role == "cto"
        assert p.stance == "reject"
        assert p.rationale == "too risky"


# ---------------------------------------------------------------------------
# synthesize — explicit decision
# ---------------------------------------------------------------------------


class TestSynthesizeExplicitDecision:
    def test_returns_provided_decision(self) -> None:
        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="high ROI"),
            Position(role="cto", stance="reject", rationale="complexity"),
        ]
        result = svc.synthesize(
            topic="Use LLM for code review",
            positions=positions,
            decision="adopt — CEO override",
            consequences="CTO concerns tracked as tech debt",
        )
        assert isinstance(result, DebateResult)
        assert result.decision == "adopt — CEO override"
        assert result.consequences == "CTO concerns tracked as tech debt"
        assert result.topic == "Use LLM for code review"
        assert len(result.positions) == 2

    def test_returns_provided_consequences(self) -> None:
        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="fast"),
        ]
        result = svc.synthesize(
            topic="Deploy to prod",
            positions=positions,
            decision="adopt",
            consequences="Monitor closely for one week",
        )
        assert result.consequences == "Monitor closely for one week"


# ---------------------------------------------------------------------------
# synthesize — majority-stance rule (decision=None)
# ---------------------------------------------------------------------------


class TestSynthesizeMajorityRule:
    def test_majority_stance_wins(self) -> None:
        """2 of 3 roles share 'adopt' stance -> that stance is chosen."""
        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="cost savings"),
            Position(role="cto", stance="adopt", rationale="proven tech"),
            Position(role="ciso", stance="reject", rationale="security risk"),
        ]
        result = svc.synthesize(
            topic="Migrate to cloud",
            positions=positions,
        )
        assert "adopt" in result.decision.lower()

    def test_tie_goes_to_first_by_input_order(self) -> None:
        """Tie (1 adopt, 1 reject) -> first position's stance wins."""
        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="fast"),
            Position(role="cto", stance="reject", rationale="risky"),
        ]
        result = svc.synthesize(
            topic="Deploy microservices",
            positions=positions,
        )
        assert "adopt" in result.decision.lower()

    def test_dissenting_positions_appear_in_consequences(self) -> None:
        """Auto-generated consequences should mention dissenting role."""
        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="growth"),
            Position(role="cto", stance="adopt", rationale="scalable"),
            Position(role="ciso", stance="reject", rationale="privacy"),
        ]
        result = svc.synthesize(
            topic="AI feature rollout",
            positions=positions,
        )
        # ciso dissented — should appear in consequences
        assert "ciso" in result.consequences.lower()

    def test_unanimous_no_dissent_in_consequences(self) -> None:
        """When all agree, consequences mentions no dissenting positions."""
        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="value"),
            Position(role="cto", stance="adopt", rationale="solid"),
        ]
        result = svc.synthesize(
            topic="Switch CI to GitHub Actions",
            positions=positions,
        )
        assert result.consequences  # not empty


# ---------------------------------------------------------------------------
# to_adr — passes options as list[str]
# ---------------------------------------------------------------------------


class TestToAdr:
    def test_to_adr_dry_run_returns_dict(self, tmp_path: Path) -> None:
        vault = _make_adr_vault(tmp_path)
        svc = DebateService(vault_path=vault, dry_run=True)
        positions = [
            Position(role="qwen", stance="adopt", rationale="fast inference"),
            Position(role="kimi", stance="defer", rationale="needs more data"),
        ]
        result = svc.synthesize(
            topic="CEO model selection",
            positions=positions,
        )
        emit = svc.to_adr(result, security_impact="None", review_date="2027-01-01")
        assert emit is not None
        assert emit.get("dry_run") is True
        content = emit["content"]
        # Each position's role and stance must appear
        assert "qwen" in content
        assert "kimi" in content
        assert "adopt" in content
        assert "defer" in content

    def test_to_adr_options_lists_each_position(self, tmp_path: Path) -> None:
        """Options passed to write_adr are one entry per position."""
        vault = _make_adr_vault(tmp_path)
        svc = DebateService(vault_path=vault, dry_run=True)
        positions = [
            Position(role="ceo", stance="adopt", rationale="strategic"),
            Position(role="cto", stance="adopt", rationale="technical"),
            Position(role="ciso", stance="reject", rationale="security"),
        ]
        result = svc.synthesize(
            topic="Adopt new LLM",
            positions=positions,
        )
        emit = svc.to_adr(result)
        assert emit is not None
        content = emit["content"]
        # All three role:stance entries should appear in the ADR options section
        assert "ceo: adopt" in content
        assert "cto: adopt" in content
        assert "ciso: reject" in content

    def test_to_adr_no_vault_returns_none(self) -> None:
        svc = DebateService(vault_path=None, dry_run=True)
        positions = [
            Position(role="ceo", stance="adopt", rationale="yes"),
        ]
        result = svc.synthesize(topic="Any topic", positions=positions)
        assert svc.to_adr(result) is None


# ---------------------------------------------------------------------------
# run — convenience wrapper
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_no_vault_returns_none(self) -> None:
        svc = DebateService(vault_path=None)
        positions = [
            Position(role="ceo", stance="adopt", rationale="growth"),
        ]
        result = svc.run(
            topic="Feature X",
            positions=positions,
        )
        assert result is None

    def test_run_dry_run_returns_dict(self, tmp_path: Path) -> None:
        vault = _make_adr_vault(tmp_path)
        svc = DebateService(vault_path=vault, dry_run=True)
        positions = [
            Position(role="ceo", stance="adopt", rationale="ROI"),
            Position(role="cto", stance="reject", rationale="risk"),
        ]
        result = svc.run(
            topic="New product launch",
            positions=positions,
            security_impact="Low",
            review_date="2027-06-01",
        )
        assert result is not None
        assert result.get("dry_run") is True

    def test_run_dry_true_writes_no_file(self, tmp_path: Path) -> None:
        vault = _make_adr_vault(tmp_path)
        svc = DebateService(vault_path=vault, dry_run=True)
        positions = [
            Position(role="ceo", stance="adopt", rationale="speed"),
        ]
        svc.run(topic="Ship it", positions=positions)
        # No real files should be created
        decisions_dir = vault / _ADR_FOLDER
        created = list(decisions_dir.iterdir())
        assert created == [], "dry_run must not write any file"

    def test_run_dry_false_writes_file(self, tmp_path: Path) -> None:
        vault = _make_adr_vault(tmp_path)
        svc = DebateService(vault_path=vault, dry_run=False)
        positions = [
            Position(role="ceo", stance="adopt", rationale="value"),
            Position(role="cto", stance="defer", rationale="later"),
        ]
        result = svc.run(
            topic="Launch MVP",
            positions=positions,
            security_impact="None",
            review_date="2027-01-01",
        )
        assert result is not None
        assert result.get("dry_run") is False
        # A file must exist under 03 - Decisions
        decisions_dir = vault / _ADR_FOLDER
        files = list(decisions_dir.iterdir())
        assert len(files) == 1
        assert files[0].suffix == ".md"
        # Content must contain topic
        assert "Launch MVP" in files[0].read_text(encoding="utf-8")
