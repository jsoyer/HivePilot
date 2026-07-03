"""
Tests for the obsidian CLI sub-app added to hivepilot.cli.

The full CLI imports the Orchestrator which in turn imports optional heavy
dependencies (langchain, etc.).  We stub those out at the module level so
the test suite stays lightweight and doesn't require the full [full] extras.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli
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
        # Prefer the real module when installed so flat MagicMock stubs do not
        # shadow proper packages (e.g. fastapi) for later tests like test_pentest.
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_vault(tmp_path: Path) -> Path:
    """Create a minimal fake Obsidian vault for CLI tests."""
    vault = tmp_path / "TestVault"
    vault.mkdir()
    for folder in [
        "00 - Inbox",
        "01 - Journal",
        "03 - Decisions",
        "08 - Security",
        "02 - Architecture",
        "12 - HivePilot",
        "99 - Archive",
    ]:
        (vault / folder).mkdir()
    for sub in ["Agents", "Tasks", "Reports", "Runs", "Interactions"]:
        (vault / "12 - HivePilot" / sub).mkdir(parents=True, exist_ok=True)
    return vault


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestObsidianCli:
    def test_obsidian_audit_command_exists(self, fake_vault: Path) -> None:
        """hivepilot obsidian audit should exit 0 and print a report."""
        runner = CliRunner()
        result = runner.invoke(app, ["obsidian", "audit", "--vault", str(fake_vault)])
        assert result.exit_code == 0, result.output

    def test_obsidian_audit_shows_present_folders(self, fake_vault: Path) -> None:
        """Audit output mentions present folders."""
        runner = CliRunner()
        result = runner.invoke(app, ["obsidian", "audit", "--vault", str(fake_vault)])
        assert "present" in result.output.lower() or "12 - HivePilot" in result.output

    def test_obsidian_audit_shows_missing_folders(self, fake_vault: Path) -> None:
        """Audit output reports missing expected folders."""
        runner = CliRunner()
        result = runner.invoke(app, ["obsidian", "audit", "--vault", str(fake_vault)])
        assert result.exit_code == 0
        # We have a partial vault so some folders should be missing
        assert "missing" in result.output.lower() or "04 - Engineering" in result.output
