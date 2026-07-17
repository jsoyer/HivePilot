"""Tests for the mandatory-agent install/doctor guardrail -- CLI wiring.

Covers `hivepilot init` (warns -- never hard-fails -- when none of
claude/codex/vibe are on PATH, since `init` scaffolds the config you need
before you can install an agent CLI in the first place; a softer warning
when only a non-claude agent is present) and `hivepilot doctor`
(mandatory-agent verdict + optional OPENROUTER_API_KEY note).

Matrix unit tests for `hivepilot.services.agent_checks.check_mandatory_agents()`
itself (PATH permutations) live in `tests/test_agent_checks.py` -- split out
to satisfy the TDD pre-write hook, which resolves the expected test path
from the production module name (`agent_checks.py` -> `test_agent_checks.py`).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli -- same
# approach as tests/test_cli.py / tests/test_init_service.py, needed because
# hivepilot.cli transitively imports hivepilot.orchestrator which imports
# several optional extras.
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402


def _fake_which(present: set[str]):
    def _which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in present else None

    return _which


# ---------------------------------------------------------------------------
# `hivepilot init` -- hard-fail / warning wiring
# ---------------------------------------------------------------------------


def test_init_exits_zero_with_warning_when_no_mandatory_agent_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`init`'s job is to scaffold config so an agent CLI can be installed
    next -- it must warn, not hard-fail, when none of claude/codex/vibe are
    on PATH (e.g. a fresh machine or CI). Run-time enforcement before an
    actual pipeline run is a separate concern, unaffected by this."""
    monkeypatch.setattr(shutil, "which", _fake_which(set()))

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    assert "claude" in result.output.lower()
    assert "codex" in result.output.lower()
    assert "vibe" in result.output.lower()


def test_init_exits_zero_with_warning_when_only_codex_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"codex"}))

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    assert "claude" in result.output.lower()


def test_init_exits_zero_with_warning_when_only_vibe_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"vibe"}))

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()


def test_init_exits_zero_cleanly_when_claude_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"claude"}))

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    # No warning needed -- claude (the strongest prerequisite) is present.
    assert "warning" not in result.output.lower()


def test_init_exits_zero_when_all_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"claude", "codex", "vibe"}))

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# `hivepilot doctor` -- verdict + optional OPENROUTER_API_KEY note
# ---------------------------------------------------------------------------


def test_doctor_reports_mandatory_agent_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"claude"}))

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "mandatory agent" in result.output.lower()
    assert "claude" in result.output.lower()


def test_doctor_reports_openrouter_api_key_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "openrouter_api_key" in result.output.lower()


def test_doctor_openrouter_api_key_shows_set_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-value")
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "sk-test-value" not in result.output  # never print the secret value
    lowered = result.output.lower()
    assert "openrouter_api_key" in lowered
    assert "set" in lowered
