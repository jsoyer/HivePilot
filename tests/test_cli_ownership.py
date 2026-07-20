"""Tests for `hivepilot ownership check` (Phase 16 C1 — read-only CLI).

`hivepilot.cli._git_changed_files` (the git subprocess call) is monkeypatched
so no real git repo state is required. Covers:

1. `--role R` + an ownership.yaml + changed files owned by another role ->
   conflicts table printed, exit code 1.
2. `--role R` + changed files all owned by R (or unowned) -> clean, exit 0.
3. No ownership.yaml present -> graceful message, exit 0 (no crash).
4. Not a git repo / git diff fails -> graceful message, exit 0.
5. No `--role` given -> advisory report of changed files matched by ANY
   role's ownership glob, always exit 0 (never flags a conflict without a
   `--role` to compare against).
6. Read-only: never mutates anything (no write/patch calls asserted implicitly
   by the mocked service never being called with a mutating method — this
   module has no mutating functions to begin with).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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

import hivepilot.cli as cli_module  # noqa: E402
from hivepilot.cli import app  # noqa: E402


def _write_ownership(tmp_path: Path) -> Path:
    path = tmp_path / "ownership.yaml"
    path.write_text(
        "backend:\n  - hivepilot/**\ndocs:\n  - docs/**\n",
        encoding="utf-8",
    )
    return path


class TestOwnershipCheckWithRole:
    def test_conflicts_print_table_and_exit_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ownership_path = _write_ownership(tmp_path)
        monkeypatch.setattr(
            cli_module, "_git_changed_files", lambda ref: ["hivepilot/x.py", "README.md"]
        )
        runner = CliRunner()

        result = runner.invoke(
            app,
            [
                "ownership",
                "check",
                "--ownership",
                str(ownership_path),
                "--role",
                "docs",
            ],
        )

        assert result.exit_code == 1, result.output
        assert "hivepilot/x.py" in result.output
        assert "backend" in result.output
        assert "docs" in result.output

    def test_clean_exits_0(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        ownership_path = _write_ownership(tmp_path)
        monkeypatch.setattr(cli_module, "_git_changed_files", lambda ref: ["hivepilot/x.py"])
        runner = CliRunner()

        result = runner.invoke(
            app,
            [
                "ownership",
                "check",
                "--ownership",
                str(ownership_path),
                "--role",
                "backend",
            ],
        )

        assert result.exit_code == 0, result.output

    def test_unowned_changed_file_is_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ownership_path = _write_ownership(tmp_path)
        monkeypatch.setattr(cli_module, "_git_changed_files", lambda ref: ["README.md"])
        runner = CliRunner()

        result = runner.invoke(
            app,
            [
                "ownership",
                "check",
                "--ownership",
                str(ownership_path),
                "--role",
                "docs",
            ],
        )

        assert result.exit_code == 0, result.output


class TestOwnershipCheckAdvisory:
    def test_no_role_reports_advisory_and_exits_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ownership_path = _write_ownership(tmp_path)
        monkeypatch.setattr(
            cli_module, "_git_changed_files", lambda ref: ["hivepilot/x.py", "unowned/y.py"]
        )
        runner = CliRunner()

        result = runner.invoke(app, ["ownership", "check", "--ownership", str(ownership_path)])

        assert result.exit_code == 0, result.output
        assert "hivepilot/x.py" in result.output
        assert "backend" in result.output
        assert "unowned/y.py" not in result.output


class TestOwnershipCheckGraceful:
    def test_missing_ownership_file_exits_0_with_clear_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli_module, "_git_changed_files", lambda ref: ["hivepilot/x.py"])
        runner = CliRunner()

        result = runner.invoke(
            app,
            [
                "ownership",
                "check",
                "--ownership",
                str(tmp_path / "does-not-exist.yaml"),
            ],
        )

        assert result.exit_code == 0, result.output
        assert result.output.strip()

    def test_malformed_ownership_file_exits_1_with_clear_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bad_path = tmp_path / "ownership.yaml"
        bad_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        monkeypatch.setattr(cli_module, "_git_changed_files", lambda ref: ["hivepilot/x.py"])
        runner = CliRunner()

        result = runner.invoke(app, ["ownership", "check", "--ownership", str(bad_path)])

        assert result.exit_code == 1

    def test_not_a_git_repo_exits_0_gracefully(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ownership_path = _write_ownership(tmp_path)

        def _raise(ref: str) -> list[str]:
            raise RuntimeError("Not a git repository (or any of the parent directories)")

        monkeypatch.setattr(cli_module, "_git_changed_files", _raise)
        runner = CliRunner()

        result = runner.invoke(app, ["ownership", "check", "--ownership", str(ownership_path)])

        assert result.exit_code == 0, result.output

    def test_no_changed_files_exits_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ownership_path = _write_ownership(tmp_path)
        monkeypatch.setattr(cli_module, "_git_changed_files", lambda ref: [])
        runner = CliRunner()

        result = runner.invoke(app, ["ownership", "check", "--ownership", str(ownership_path)])

        assert result.exit_code == 0, result.output
