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


def _write_minimal_valid_config(base_dir: Path) -> None:
    """Write the six required config files (+ a prompts/agents dir) so
    validate_config() reports zero cross-reference problems."""
    import yaml

    (base_dir / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"path": "~/dev/demo"}}})
    )
    (base_dir / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (base_dir / "prompts" / "agents").mkdir(parents=True)
    (base_dir / "prompts" / "agents" / "planner.md").write_text("# planner")


class TestValidateCli:
    """`hivepilot validate` -- default (no --dir) must resolve the config
    that's actually active (XDG -> config_repo -> base_dir, matching
    `hivepilot config sync`'s real write target and every runtime loader),
    not literally `Path.cwd()`. An explicit `--dir` must keep validating
    that exact directory, unaffected by any unrelated XDG config."""

    def test_default_no_dir_resolves_config_synced_to_xdg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: after `hivepilot config sync` (writes to XDG_CONFIG_HOME),
        running bare `hivepilot validate` from an unrelated cwd must report OK,
        not false 'Missing required config file' errors."""
        xdg_dir = tmp_path / "xdg" / "hivepilot"
        xdg_dir.mkdir(parents=True)
        _write_minimal_valid_config(xdg_dir)

        empty_cwd = tmp_path / "empty-cwd"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        runner = CliRunner()
        result = runner.invoke(app, ["validate"])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_explicit_dir_still_validates_that_exact_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--dir X` must keep validating X literally -- even when an
        unrelated XDG config exists -- so scaffold/pre-activation validation
        (and the documented `--dir /data` deploy flow) is unaffected."""
        xdg_dir = tmp_path / "xdg" / "hivepilot"
        xdg_dir.mkdir(parents=True)
        _write_minimal_valid_config(xdg_dir)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        explicit_dir = tmp_path / "explicit-target"
        explicit_dir.mkdir()  # deliberately empty -- no config files here

        runner = CliRunner()
        result = runner.invoke(app, ["validate", "--dir", str(explicit_dir)])

        assert result.exit_code == 1, result.output
        assert "Missing required config file" in result.output

    def test_explicit_dir_with_valid_config_still_passes(self, tmp_path: Path) -> None:
        """Regression guard: explicit --dir with a valid config must still
        report OK exactly as before this fix."""
        _write_minimal_valid_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ["validate", "--dir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output


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
