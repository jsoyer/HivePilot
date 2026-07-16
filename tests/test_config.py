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
# Phase 24b.2a — claude_capture_usage (opt-in usage capture)
# ---------------------------------------------------------------------------


class TestClaudeCaptureUsage:
    """`claude_capture_usage` defaults to False (byte-identical behaviour) and
    is env-overridable (HIVEPILOT_CLAUDE_CAPTURE_USAGE)."""

    def test_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_CLAUDE_CAPTURE_USAGE", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.claude_capture_usage is False

    def test_env_override_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_CLAUDE_CAPTURE_USAGE", "true")
        s = Settings()
        assert s.claude_capture_usage is True

    def test_env_override_false_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_CLAUDE_CAPTURE_USAGE", "false")
        s = Settings()
        assert s.claude_capture_usage is False


# ---------------------------------------------------------------------------
# headroom plugin — headroom_enabled
# ---------------------------------------------------------------------------


class TestEnableTracing:
    """`enable_tracing` (Phase 18) defaults to False — ships dormant, mirrors
    `enable_webui`/`headroom_enabled`'s opt-in gating — and is env-overridable.
    `otel_exporter_otlp_endpoint` defaults to None (the OTel SDK falls back to
    reading the standard `OTEL_EXPORTER_OTLP_ENDPOINT` env var natively when
    unset). `otel_service_name` defaults to "hivepilot"."""

    def test_enable_tracing_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_ENABLE_TRACING", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.enable_tracing is False

    def test_enable_tracing_env_override_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_ENABLE_TRACING", "true")
        s = Settings()
        assert s.enable_tracing is True

    def test_otel_exporter_otlp_endpoint_default_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HIVEPILOT_OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.otel_exporter_otlp_endpoint is None

    def test_otel_exporter_otlp_endpoint_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HIVEPILOT_OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        s = Settings()
        assert s.otel_exporter_otlp_endpoint == "http://collector:4317"

    def test_otel_service_name_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_OTEL_SERVICE_NAME", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.otel_service_name == "hivepilot"

    def test_otel_service_name_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_OTEL_SERVICE_NAME", "hivepilot-staging")
        s = Settings()
        assert s.otel_service_name == "hivepilot-staging"


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


# ---------------------------------------------------------------------------
# llm_price_map — Phase 24b.2b cost/provider analytics price-map override
# ---------------------------------------------------------------------------


class TestLlmPriceMap:
    """`llm_price_map` defaults to None (pricing.py's default table applies
    unmodified) and is env-overridable via a JSON object."""

    def test_default_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_LLM_PRICE_MAP", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.llm_price_map is None

    def test_env_override_parses_json_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "HIVEPILOT_LLM_PRICE_MAP",
            '{"my-model": {"input": 1.0, "output": 2.0}}',
        )
        s = Settings()
        assert s.llm_price_map == {"my-model": {"input": 1.0, "output": 2.0}}


# ---------------------------------------------------------------------------
# Plugin enable/disable — plugins_disabled (Sprint 5)
# ---------------------------------------------------------------------------


class TestPluginsDisabled:
    """`plugins_disabled` defaults to an empty list (no plugin skipped) and
    is env-overridable — complements `plugins_enabled`'s master switch with
    a per-plugin skip list."""

    def test_default_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_PLUGINS_DISABLED", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.plugins_disabled == []

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_PLUGINS_DISABLED", '["rtk", "obsidian"]')
        s = Settings()
        assert s.plugins_disabled == ["rtk", "obsidian"]

    def test_is_list_of_str_type(self) -> None:
        s = Settings()
        assert isinstance(s.plugins_disabled, list)
