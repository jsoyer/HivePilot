"""Tests for the `hivepilot agents` CLI (S2) + `doctor` install-hint
integration.

`hivepilot agents install` is a thin CLI wrapper around
`hivepilot.services.agent_install.propose_install` (see
`tests/test_agent_install.py` for that module's own security matrix). The
single most important property under test here, at the CLI layer, is that
this wrapper NEVER forces interactivity: it must always call
`propose_install(spec, assume_yes=<flag>, interactive=None)` so
`propose_install`'s own real-TTY auto-detection is what decides whether
anything executes -- `--yes` only ever maps to `assume_yes`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli -- same
# approach as tests/test_cli.py / tests/test_mandatory_agents.py.
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

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402
from hivepilot.services.agent_install import AGENT_INSTALL_SPECS, InstallResult  # noqa: E402


def _fake_which(present: set[str]):
    def _which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in present else None

    return _which


# ---------------------------------------------------------------------------
# `hivepilot agents list`
# ---------------------------------------------------------------------------


def test_agents_list_renders_row_per_kind_with_path_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"claude"}))

    runner = CliRunner()
    result = runner.invoke(app, ["agents", "list"])

    assert result.exit_code == 0, result.output
    assert "claude" in result.output
    assert "codex" in result.output


def test_agents_list_shows_pinned_vs_docs_only_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which(set()))

    runner = CliRunner()
    result = runner.invoke(app, ["agents", "list"])

    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    # claude has a pinned command; gh is docs-only (command=None).
    assert "pinned" in lowered
    assert "docs" in lowered


def test_agents_list_never_invokes_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`agents list` is read-only -- must never shell out."""
    monkeypatch.setattr(shutil, "which", _fake_which(set()))
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["agents", "list"])

    assert result.exit_code == 0, result.output
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# `hivepilot agents install <unknown>`
# ---------------------------------------------------------------------------


def test_agents_install_unknown_kind_exits_1_with_friendly_message() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["agents", "install", "not-a-real-agent"])

    assert result.exit_code == 1
    assert "unknown" in result.output.lower()
    assert "not-a-real-agent" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# `hivepilot agents install <docs-only-kind>`
# ---------------------------------------------------------------------------


def test_agents_install_docs_only_kind_never_runs_subprocess() -> None:
    assert AGENT_INSTALL_SPECS["gh"].command is None  # sanity: gh is docs-only

    mock_run = MagicMock()
    with patch("subprocess.run", mock_run):
        runner = CliRunner()
        result = runner.invoke(app, ["agents", "install", "gh", "--yes"])

    assert result.exit_code == 0, result.output
    assert AGENT_INSTALL_SPECS["gh"].docs_url in result.output
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# `hivepilot agents install <pinned-kind>` -- non-interactive refusal
# (THE key security test at the CLI layer)
# ---------------------------------------------------------------------------


def test_agents_install_pinned_kind_non_tty_refuses_even_with_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    runner = CliRunner()
    result = runner.invoke(app, ["agents", "install", "claude", "--yes"])

    assert result.exit_code == 0, result.output
    assert "non-interactive" in result.output.lower()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# `hivepilot agents install <pinned-kind>` -- interactive TTY + consent
# ---------------------------------------------------------------------------


def test_agents_install_pinned_kind_interactive_yes_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Click's `CliRunner` always replaces `sys.stdin`/`sys.stdout` with its
    own (non-TTY, non-monkeypatchable-instance -- `io.TextIOWrapper` is a C
    immutable type) capture streams, so driving the REAL `isatty()` branch of
    `propose_install` end-to-end through the CLI isn't feasible here; that
    interactive-execution path (exact argv, prompt handling) is already
    covered directly against `propose_install` in `tests/test_agent_install.py`.
    This test instead locks the CLI-layer contract: once `propose_install`
    reports `ran=True`, the CLI must exercise/forward that outcome (print its
    message, exit 0) -- confirming the install path is genuinely wired
    through and not swallowed."""
    mock_propose = MagicMock(
        return_value=InstallResult(
            ran=True, exit_code=0, message="Claude Code installer exited with code 0"
        )
    )
    with patch("hivepilot.services.agent_install.propose_install", mock_propose):
        runner = CliRunner()
        result = runner.invoke(app, ["agents", "install", "claude"])

    assert result.exit_code == 0, result.output
    assert "installer exited with code 0" in result.output
    mock_propose.assert_called_once()
    _args, kwargs = mock_propose.call_args
    assert kwargs.get("interactive") is None
    assert kwargs.get("assume_yes") is False


def test_agents_install_yes_flag_skips_prompt_when_already_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--yes` maps to `assume_yes=True` -- inside a real, already-interactive
    `propose_install` call this skips its y/N prompt (locked at the unit
    level by `test_propose_install_interactive_assume_yes_skips_prompt` in
    `tests/test_agent_install.py`). At the CLI layer, assert the flag is
    forwarded correctly and the successful outcome is surfaced."""
    mock_propose = MagicMock(
        return_value=InstallResult(
            ran=True, exit_code=0, message="Claude Code installer exited with code 0"
        )
    )
    with patch("hivepilot.services.agent_install.propose_install", mock_propose):
        runner = CliRunner()
        result = runner.invoke(app, ["agents", "install", "claude", "--yes"])

    assert result.exit_code == 0, result.output
    mock_propose.assert_called_once()
    _args, kwargs = mock_propose.call_args
    assert kwargs.get("assume_yes") is True


def test_agents_install_nonzero_exit_code_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_propose = MagicMock(
        return_value=InstallResult(
            ran=True, exit_code=7, message="Claude Code installer exited with code 7"
        )
    )
    with patch("hivepilot.services.agent_install.propose_install", mock_propose):
        runner = CliRunner()
        result = runner.invoke(app, ["agents", "install", "claude", "--yes"])

    assert result.exit_code == 7


# ---------------------------------------------------------------------------
# CRITICAL: the CLI must never pass interactive=True (or anything other than
# None, letting propose_install auto-detect) -- and --yes must map ONLY to
# assume_yes.
# ---------------------------------------------------------------------------


def test_agents_install_never_passes_interactive_true() -> None:
    mock_propose = MagicMock(
        return_value=InstallResult(ran=False, exit_code=None, message="declined by operator")
    )
    with patch("hivepilot.services.agent_install.propose_install", mock_propose):
        runner = CliRunner()
        result = runner.invoke(app, ["agents", "install", "claude", "--yes"])

    assert result.exit_code == 0, result.output
    mock_propose.assert_called_once()
    _args, kwargs = mock_propose.call_args
    assert kwargs.get("interactive") is None
    assert kwargs.get("assume_yes") is True


def test_agents_install_without_yes_flag_assume_yes_is_false() -> None:
    mock_propose = MagicMock(
        return_value=InstallResult(ran=False, exit_code=None, message="declined by operator")
    )
    with patch("hivepilot.services.agent_install.propose_install", mock_propose):
        runner = CliRunner()
        result = runner.invoke(app, ["agents", "install", "claude"])

    assert result.exit_code == 0, result.output
    mock_propose.assert_called_once()
    _args, kwargs = mock_propose.call_args
    assert kwargs.get("interactive") is None
    assert kwargs.get("assume_yes") is False


# ---------------------------------------------------------------------------
# `doctor` install-hint integration
# ---------------------------------------------------------------------------


def test_doctor_suggests_install_command_for_missing_pinned_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which(set()))

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "hivepilot agents install claude" in result.output


def test_doctor_suggests_docs_url_for_missing_docs_only_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which(set()))

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert AGENT_INSTALL_SPECS["gh"].docs_url in result.output


def test_doctor_no_suggestion_when_all_agents_present(monkeypatch: pytest.MonkeyPatch) -> None:
    all_binaries = {spec.binary for spec in AGENT_INSTALL_SPECS.values()}
    monkeypatch.setattr(shutil, "which", _fake_which(all_binaries))

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "hivepilot agents install" not in result.output


def test_doctor_never_invokes_propose_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """doctor is advisory-only -- it must never call propose_install itself."""
    monkeypatch.setattr(shutil, "which", _fake_which(set()))
    mock_propose = MagicMock()
    with patch("hivepilot.services.agent_install.propose_install", mock_propose):
        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    mock_propose.assert_not_called()
