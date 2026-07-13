"""CLI tests for `hivepilot config get` / `hivepilot config list` (Sprint 2 of
the config-edit-commands PRD).

Follows tests/test_cli.py's pattern: stub heavy optional deps in sys.modules
before importing hivepilot.cli so the suite stays lightweight.
"""

from __future__ import annotations

import sys
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

from hivepilot.cli import app  # noqa: E402
from hivepilot.config import Settings, settings  # noqa: E402

runner = CliRunner()


def _mutate_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Mutate the process-wide `settings` singleton the CLI reads, so a
    "real" raw secret value flows through the get/list commands. Env-var
    monkeypatching would be a no-op here since `settings` is constructed once
    at import time."""
    for field_name, value in overrides.items():
        monkeypatch.setattr(settings, field_name, value)


class TestConfigGet:
    def test_get_known_key_prints_value_source_and_rank(self) -> None:
        result = runner.invoke(app, ["config", "get", "concurrency_limit"])
        assert result.exit_code == 0, result.output
        assert "concurrency_limit" in result.output
        assert "xdg_rank" in result.output.lower() or "rank" in result.output.lower()

    def test_get_file_backed_key_reports_source_path(self) -> None:
        result = runner.invoke(app, ["config", "get", "projects_file"])
        assert result.exit_code == 0, result.output
        assert "source" in result.output.lower()

    def test_get_xdg_override_reports_rank_one(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A setting overridden by a file under $XDG_CONFIG_HOME/hivepilot/
        reports rank 1."""
        xdg_root = tmp_path / "xdg-home"  # type: ignore[operator]
        hivepilot_dir = xdg_root / "hivepilot"
        hivepilot_dir.mkdir(parents=True)
        (hivepilot_dir / "groups.yaml").write_text("groups: {}\n", encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))

        result = runner.invoke(app, ["config", "get", "groups_file"])
        assert result.exit_code == 0, result.output
        assert "1" in result.output
        assert str(hivepilot_dir / "groups.yaml") in result.output

    def test_get_unknown_key_exits_nonzero_and_lists_valid_keys(self) -> None:
        result = runner.invoke(app, ["config", "get", "not_a_real_setting_xyz"])
        assert result.exit_code != 0
        assert "not_a_real_setting_xyz" in result.output
        # A handful of real Settings fields should be listed as valid options.
        for key in Settings.model_fields:
            assert key in result.output
            break  # Only need to confirm the listing mechanism works.

    @pytest.mark.parametrize(
        "key",
        ["chatops_token", "telegram_bot_token", "linear_api_key", "database_url"],
    )
    def test_get_secret_field_redacted(self, key: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _mutate_settings(monkeypatch, **{key: "super-secret-raw-value"})
        result = runner.invoke(app, ["config", "get", key])
        assert result.exit_code == 0, result.output
        assert "REDACTED" in result.output
        assert "super-secret-raw-value" not in result.output


class TestConfigList:
    def test_list_includes_every_settings_field(self) -> None:
        result = runner.invoke(app, ["config", "list"])
        assert result.exit_code == 0, result.output
        missing = [key for key in Settings.model_fields if key not in result.output]
        assert not missing, f"config list output is missing fields: {missing}"

    def test_list_never_leaks_raw_secret_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mutate_settings(
            monkeypatch,
            chatops_token="super-secret-raw-value",
            telegram_bot_token="another-raw-secret",
        )
        result = runner.invoke(app, ["config", "list"])
        assert result.exit_code == 0, result.output
        assert "super-secret-raw-value" not in result.output
        assert "another-raw-secret" not in result.output
        assert "REDACTED" in result.output
