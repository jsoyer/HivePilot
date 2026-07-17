"""Unit tests for `hivepilot.services.agent_checks.check_mandatory_agents()`.

Named to mirror `hivepilot/services/agent_checks.py` (matches this repo's
`test_<module>.py` convention) and to satisfy the TDD pre-write hook, which
resolves the expected test path from the production module name.

CLI-level wiring tests (`hivepilot init` hard-fail, `hivepilot doctor`
verdict) live in `tests/test_mandatory_agents.py` per the sprint spec.
"""

from __future__ import annotations

import shutil
from typing import Optional

import pytest

from hivepilot.services import agent_checks


def _fake_which(present: set[str]):
    def _which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in present else None

    return _which


def test_mandatory_agents_constant_is_exactly_claude_codex_vibe() -> None:
    assert agent_checks.MANDATORY_AGENTS == ("claude", "codex", "vibe")


def test_check_mandatory_agents_none_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which(set()))
    report = agent_checks.check_mandatory_agents()
    assert report.present == []
    assert report.claude_ok is False
    assert report.any_ok is False


def test_check_mandatory_agents_only_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"claude"}))
    report = agent_checks.check_mandatory_agents()
    assert report.present == ["claude"]
    assert report.claude_ok is True
    assert report.any_ok is True


def test_check_mandatory_agents_only_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"codex"}))
    report = agent_checks.check_mandatory_agents()
    assert report.present == ["codex"]
    assert report.claude_ok is False
    assert report.any_ok is True


def test_check_mandatory_agents_only_vibe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"vibe"}))
    report = agent_checks.check_mandatory_agents()
    assert report.present == ["vibe"]
    assert report.claude_ok is False
    assert report.any_ok is True


def test_check_mandatory_agents_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"claude", "codex", "vibe"}))
    report = agent_checks.check_mandatory_agents()
    assert report.present == ["claude", "codex", "vibe"]
    assert report.claude_ok is True
    assert report.any_ok is True


def test_check_mandatory_agents_preserves_declared_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`present` should follow MANDATORY_AGENTS order, not PATH scan order."""
    monkeypatch.setattr(shutil, "which", _fake_which({"vibe", "claude"}))
    report = agent_checks.check_mandatory_agents()
    assert report.present == ["claude", "vibe"]
