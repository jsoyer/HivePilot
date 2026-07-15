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
        # old deployment-specific absolute path.  Operators override via HIVEPILOT_OBSIDIAN_VAULT.
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


# ---------------------------------------------------------------------------
# PRD A2 Sprint 2 — context_routing_mode
# ---------------------------------------------------------------------------


class TestContextRoutingMode:
    """`context_routing_mode` defaults to "full" (today's behaviour for all
    roles) and is env-overridable to "keyed" (opt-in)."""

    def test_default_is_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_CONTEXT_ROUTING_MODE", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.context_routing_mode == "full"

    def test_env_override_keyed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_CONTEXT_ROUTING_MODE", "keyed")
        s = Settings()
        assert s.context_routing_mode == "keyed"

    def test_invalid_value_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only "full" | "keyed" are valid — anything else must fail pydantic
        validation (Literal-typed field), not silently coerce."""
        monkeypatch.setenv("HIVEPILOT_CONTEXT_ROUTING_MODE", "bogus")
        with pytest.raises(Exception):  # pydantic ValidationError
            Settings()


# ---------------------------------------------------------------------------
# headroom plugin — headroom_enabled
# ---------------------------------------------------------------------------


class TestHeadroomEnabled:
    """`headroom_enabled` defaults to False (ships dormant, mirrors
    `context_routing_mode`'s opt-in gating) and is env-overridable."""

    def test_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_HEADROOM_ENABLED", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.headroom_enabled is False

    def test_env_override_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_HEADROOM_ENABLED", "true")
        s = Settings()
        assert s.headroom_enabled is True


# ---------------------------------------------------------------------------
# mem0 plugin — mem0_enabled
# ---------------------------------------------------------------------------


class TestMem0Enabled:
    """`mem0_enabled` defaults to False (ships dormant, mirrors
    `headroom_enabled`'s opt-in gating) and is env-overridable."""

    def test_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_MEM0_ENABLED", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.mem0_enabled is False

    def test_env_override_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_MEM0_ENABLED", "true")
        s = Settings()
        assert s.mem0_enabled is True

    def test_api_key_defaults_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_MEM0_API_KEY", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.mem0_api_key is None

    def test_api_key_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_MEM0_API_KEY", "mk-test-123")
        s = Settings()
        assert s.mem0_api_key == "mk-test-123"

    def test_config_defaults_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_MEM0_CONFIG", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.mem0_config is None
