"""Tests for the `hivepilot setup` CLI command -- a thin wrapper over
`hivepilot.services.setup_wizard.run_setup`.

The service layer's own behavior (steps, `.env` upsert, Telegram
auto-detection, non-interactive mode, `--only` dispatch) is covered in
depth by `tests/test_setup_wizard.py`; this file only proves the CLI
wiring is correct -- flags are parsed and forwarded into a `SetupOptions`,
and the process exit code mirrors `run_setup`'s return value.
"""

from __future__ import annotations

import re
import sys
from unittest.mock import MagicMock

# Matches ANSI SGR escape sequences (color/style codes rich/typer emit even
# with force_terminal off in some CI shells) -- stripped before any
# substring assertion against --help output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _flatten_help_output(output: str) -> str:
    """Strip ANSI escapes and collapse ALL whitespace (including the
    newlines rich's bordered --help panel wraps a long flag list across at
    narrow/CI terminal widths) so a `"--flag" in flattened` membership
    check is robust to wrapping. Hyphenated flag names survive whitespace
    removal intact, so this still fails if a flag is genuinely absent."""
    plain = _ANSI_RE.sub("", output)
    return re.sub(r"\s+", "", plain)


# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli -- same
# approach as tests/test_cli_agents.py / tests/test_cli.py.
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
from hivepilot.services import setup_wizard  # noqa: E402

runner = CliRunner()


def test_setup_help_lists_key_flags() -> None:
    # Width-robust against CI's narrower/different terminal width than a
    # local shell: rich/typer wrap --help's bordered panel to the CURRENT
    # terminal width, so a long flag can get split across a line break and
    # break a naive contiguous-substring match. Belt-and-suspenders: force
    # a wide, stable width via COLUMNS (typer's rich help honors it) AND
    # normalize (strip ANSI, collapse whitespace) before asserting.
    result = runner.invoke(app, ["setup", "--help"], env={"COLUMNS": "200", "TERM": "dumb"})
    assert result.exit_code == 0
    flat = _flatten_help_output(result.output)
    assert "--non-interactive" in flat
    assert "--only" in flat
    assert "--mint-admin-token" in flat
    assert "--force" in flat


def test_setup_forwards_flags_and_return_code(monkeypatch) -> None:
    captured: dict = {}

    def _fake_run_setup(console, options) -> int:
        captured["options"] = options
        return 0

    monkeypatch.setattr(setup_wizard, "run_setup", _fake_run_setup)

    result = runner.invoke(
        app,
        [
            "setup",
            "--non-interactive",
            "--only",
            "telegram",
            "--telegram-bot-token",
            "faketoken",
        ],
    )

    assert result.exit_code == 0
    options = captured["options"]
    assert options.non_interactive is True
    assert options.only == "telegram"
    assert options.telegram_bot_token == "faketoken"
    # HIGH-2 / LOW-2: both new opt-in flags must default to the SAFE choice
    # when not explicitly passed.
    assert options.mint_admin_token is False
    assert options.force is False


def test_setup_forwards_mint_admin_token_and_force_flags(monkeypatch) -> None:
    captured: dict = {}

    def _fake_run_setup(console, options) -> int:
        captured["options"] = options
        return 0

    monkeypatch.setattr(setup_wizard, "run_setup", _fake_run_setup)

    result = runner.invoke(
        app,
        ["setup", "--non-interactive", "--only", "token", "--mint-admin-token", "--force"],
    )

    assert result.exit_code == 0
    options = captured["options"]
    assert options.mint_admin_token is True
    assert options.force is True


def test_setup_non_zero_exit_propagates(monkeypatch) -> None:
    monkeypatch.setattr(setup_wizard, "run_setup", lambda console, options: 1)
    result = runner.invoke(app, ["setup", "--non-interactive", "--only", "telegram"])
    assert result.exit_code == 1
