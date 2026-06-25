"""
Tests for hivepilot.config — verifies new obsidian_vault setting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.config import Settings


class TestObsidianVaultConfig:
    def test_obsidian_vault_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """obsidian_vault defaults to a relative 'obsidian-vault' path (deployment-agnostic)."""
        # Clear any env override so we get the true default, and skip .env to
        # avoid the deployment-specific HIVEPILOT_OBSIDIAN_VAULT value.
        monkeypatch.delenv("HIVEPILOT_OBSIDIAN_VAULT", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        # Default is now a relative path so it works on any machine without the
        # noxys-specific absolute path.  Operators override via HIVEPILOT_OBSIDIAN_VAULT.
        assert s.obsidian_vault == Path("obsidian-vault")

    def test_obsidian_vault_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HIVEPILOT_OBSIDIAN_VAULT env var overrides the default."""
        monkeypatch.setenv("HIVEPILOT_OBSIDIAN_VAULT", "/tmp/test-vault")
        s = Settings()
        assert s.obsidian_vault == Path("/tmp/test-vault")

    def test_obsidian_vault_is_path_type(self) -> None:
        """obsidian_vault field is a Path, not a string."""
        s = Settings()
        assert isinstance(s.obsidian_vault, Path)


def test_blank_notification_chat_id_is_none(monkeypatch) -> None:
    from hivepilot.config import Settings

    monkeypatch.setenv("HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID", "")
    s = Settings()
    assert s.telegram_notification_chat_id is None


def test_numeric_notification_chat_id(monkeypatch) -> None:
    from hivepilot.config import Settings

    monkeypatch.setenv("HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID", "12345")
    s = Settings()
    assert s.telegram_notification_chat_id == 12345
