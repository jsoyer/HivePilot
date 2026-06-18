"""
Tests for hivepilot.config — verifies new obsidian_vault setting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.config import Settings


class TestObsidianVaultConfig:
    def test_obsidian_vault_default(self) -> None:
        """obsidian_vault defaults to the real vault path."""
        s = Settings()
        expected = Path("/home/jeromesoyer/Documents/Github/jsoyer/obsidian-vault/Noxys")
        assert s.obsidian_vault == expected

    def test_obsidian_vault_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HIVEPILOT_OBSIDIAN_VAULT env var overrides the default."""
        monkeypatch.setenv("HIVEPILOT_OBSIDIAN_VAULT", "/tmp/test-vault")
        s = Settings()
        assert s.obsidian_vault == Path("/tmp/test-vault")

    def test_obsidian_vault_is_path_type(self) -> None:
        """obsidian_vault field is a Path, not a string."""
        s = Settings()
        assert isinstance(s.obsidian_vault, Path)
