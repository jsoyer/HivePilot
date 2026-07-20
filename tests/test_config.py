"""
Tests for hivepilot.config — verifies new obsidian_vault setting.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

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


class TestPluginsSourceConfig:
    """`plugins_source_repo` / `plugins_source_ref` — where `hivepilot plugins
    install` fetches curated built-in example plugins from (Sprint:
    plugins-install PRD)."""

    def test_plugins_source_repo_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_PLUGINS_SOURCE_REPO", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.plugins_source_repo == "https://raw.githubusercontent.com/jsoyer/HivePilot"

    def test_plugins_source_ref_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_PLUGINS_SOURCE_REF", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.plugins_source_ref == "main"

    def test_plugins_source_repo_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "HIVEPILOT_PLUGINS_SOURCE_REPO", "https://raw.githubusercontent.com/acme/fork"
        )
        s = Settings()
        assert s.plugins_source_repo == "https://raw.githubusercontent.com/acme/fork"

    def test_plugins_source_ref_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_PLUGINS_SOURCE_REF", "v1.2.3")
        s = Settings()
        assert s.plugins_source_ref == "v1.2.3"


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


class TestProjectCloneProtocol:
    """`project_clone_protocol` (auto-clone missing project repo, PR B)
    defaults to "ssh" -- byte-identical dormant default matching
    `github_service.ensure_repository`'s existing ssh default -- and is
    env-overridable via HIVEPILOT_PROJECT_CLONE_PROTOCOL."""

    def test_default_is_ssh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_PROJECT_CLONE_PROTOCOL", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.project_clone_protocol == "ssh"

    def test_env_override_https(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_PROJECT_CLONE_PROTOCOL", "https")
        s = Settings()
        assert s.project_clone_protocol == "https"


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


# ---------------------------------------------------------------------------
# Multi-directory plugin search — plugins_extra_dirs
# ---------------------------------------------------------------------------


class TestPluginsExtraDirs:
    """`plugins_extra_dirs` is an opt-in list of additional directories
    `_scan_local_plugins` (hivepilot/plugins.py) scans AFTER `base_dir/plugins`
    — lets a config repo that overrides `base_dir` (to load its own
    `plugins/*.py`) also load the engine's shipped `plugins/*.py`, instead of
    having to choose one or the other. Populated from
    HIVEPILOT_PLUGINS_EXTRA_DIRS as an os.pathsep-separated path list —
    deliberately NOT the JSON-array convention `plugins_disabled` above uses,
    since a directory list reads more naturally PATH/PYTHONPATH-style."""

    def test_default_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_PLUGINS_EXTRA_DIRS", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.plugins_extra_dirs == []

    def test_env_override_pathsep_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "HIVEPILOT_PLUGINS_EXTRA_DIRS",
            os.pathsep.join(["/opt/hivepilot/plugins", "/srv/config/plugins"]),
        )
        s = Settings()
        assert s.plugins_extra_dirs == [
            Path("/opt/hivepilot/plugins"),
            Path("/srv/config/plugins"),
        ]

    def test_empty_env_value_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_PLUGINS_EXTRA_DIRS", "")
        s = Settings()
        assert s.plugins_extra_dirs == []

    def test_is_list_of_path_type(self) -> None:
        s = Settings()
        assert isinstance(s.plugins_extra_dirs, list)


# ---------------------------------------------------------------------------
# Sprint 2 (runner-defaults-plugins-mode PRD) — gemini/opencode/ollama
# per-plugin enable flags. Mirrors herdr_enabled/infisical_enabled/
# obsidian_enabled/onepassword_enabled/rtk_enabled/sample_enabled's exact
# default-True, opt-OUT pattern: gemini/opencode/ollama moved OUT of
# _BUILTIN_RUNNERS and into PATH-gated plugins (plugins/gemini.py etc.) in
# this sprint, and each flag defaults True so existing configs referencing
# `kind: gemini`/`opencode`/`ollama` keep resolving exactly as before
# whenever the CLI binary is on PATH.
# ---------------------------------------------------------------------------


class TestGeminiOpencodeOllamaEnabledFlags:
    def test_all_three_default_to_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_GEMINI_ENABLED", raising=False)
        monkeypatch.delenv("HIVEPILOT_OPENCODE_ENABLED", raising=False)
        monkeypatch.delenv("HIVEPILOT_OLLAMA_ENABLED", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.gemini_enabled is True
        assert s.opencode_enabled is True
        assert s.ollama_enabled is True

    def test_gemini_enabled_env_override_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_GEMINI_ENABLED", "false")
        s = Settings()
        assert s.gemini_enabled is False

    def test_opencode_enabled_env_override_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_OPENCODE_ENABLED", "false")
        s = Settings()
        assert s.opencode_enabled is False

    def test_ollama_enabled_env_override_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_OLLAMA_ENABLED", "false")
        s = Settings()
        assert s.ollama_enabled is False


# ---------------------------------------------------------------------------
# Phase 25 — hugo runner plugin enable flag. Mirrors rtk_enabled's exact
# default-True, opt-OUT pattern: a brand-new, PATH-gated `kind: "hugo"`
# runner shipped directly as plugins/hugo.py.
# ---------------------------------------------------------------------------


class TestHugoEnabledFlag:
    def test_default_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_HUGO_ENABLED", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.hugo_enabled is True

    def test_env_override_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_HUGO_ENABLED", "false")
        s = Settings()
        assert s.hugo_enabled is False


# ---------------------------------------------------------------------------
# Phase 26b — plugins_capability_policy: the operator allow-list gating
# `hivepilot.plugin_capabilities.validate_capabilities`. Default `[]` =
# fail-closed deny-every-declared-capability (mirrors `plugins_disabled`'s
# JSON-array env convention).
# ---------------------------------------------------------------------------


class TestPluginsCapabilityPolicy:
    def test_default_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_PLUGINS_CAPABILITY_POLICY", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.plugins_capability_policy == []

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_PLUGINS_CAPABILITY_POLICY", '["network", "env"]')
        s = Settings()
        assert s.plugins_capability_policy == ["network", "env"]

    def test_is_list_of_str_type(self) -> None:
        s = Settings()
        assert isinstance(s.plugins_capability_policy, list)


# ---------------------------------------------------------------------------
# Phase 14c (#249) — config_hot_reload: opt-in AUTOMATIC per-tick roles.yaml
# reload in SchedulerDaemon. Mirrors plugins_hot_reload's shape; default OFF
# for byte-identical scheduler behavior. Explicit reload (CLI/admin endpoint/
# SIGHUP) is unaffected by this flag.
# ---------------------------------------------------------------------------


class TestConfigHotReloadFlag:
    def test_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_CONFIG_HOT_RELOAD", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.config_hot_reload is False

    def test_env_override_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_CONFIG_HOT_RELOAD", "true")
        s = Settings()
        assert s.config_hot_reload is True


# ---------------------------------------------------------------------------
# HIGH-severity fix — lenient CSV/JSON parsing for env `list[...]` fields.
#
# Root cause: pydantic-settings decodes a `list[...]` field's env value as
# STRICT JSON before any validator runs, UNLESS the field is
# `Annotated[..., NoDecode]`. A plain value ("123456"), a CSV value
# ("123,456"), or an EMPTY value ("") is not valid JSON, so `Settings()`
# raised `SettingsError` at import time — bricking the entire CLI/app,
# since `hivepilot.config` constructs the module-level `settings` singleton
# at import. This is the exact crash an operator hit with
# `HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS=123456`.
#
# Fix: every affected field is now `Annotated[list[X], NoDecode]` plus a
# shared `mode="before"` validator (`_parse_env_list`) that accepts empty
# (-> []), a bare/CSV string (-> split on ",", stripped), a JSON array
# string (backward-compat, existing configs keep working), or an
# already-constructed list (passthrough, e.g. tests building Settings
# directly in Python).
# ---------------------------------------------------------------------------


class TestLenientEnvListParsing:
    """Representative sample across the fix: an int-element field
    (telegram_allowed_chat_ids), a str-element field
    (slack_allowed_channel_ids), and plugins_disabled (the field the
    existing JSON-array tests above already cover, used here as the CSV/
    empty/plain regression counterpart)."""

    # -- telegram_allowed_chat_ids: list[int] -----------------------------

    def test_telegram_empty_env_value_no_longer_crashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE regression guard: before the fix, an empty env value for a
        list[...] field raised SettingsError at Settings() construction,
        bricking the whole CLI/app (hivepilot.config is imported at
        startup). After the fix, empty -> []."""
        monkeypatch.setenv("HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS", "")
        s = Settings()
        assert s.telegram_allowed_chat_ids == []

    def test_telegram_plain_single_value_coerces_to_int(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the exact operator-reported crash:
        HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS=123456 (no brackets, no
        quoting) must work, not just JSON."""
        monkeypatch.setenv("HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS", "123456")
        s = Settings()
        assert s.telegram_allowed_chat_ids == [123456]

    def test_telegram_csv_value_coerces_each_to_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS", "123,456")
        s = Settings()
        assert s.telegram_allowed_chat_ids == [123, 456]

    def test_telegram_json_array_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backward-compat: existing deployments using the pre-fix JSON
        array convention keep working unchanged."""
        monkeypatch.setenv("HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS", "[123, 456]")
        s = Settings()
        assert s.telegram_allowed_chat_ids == [123, 456]

    def test_telegram_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.telegram_allowed_chat_ids == []

    def test_telegram_malformed_element_raises_clean_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely non-int element must give a clean pydantic
        ValidationError (naming the field), not a bare JSONDecodeError and
        not a crash that takes down unrelated fields too."""
        monkeypatch.setenv("HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS", "abc")
        with pytest.raises(ValidationError) as exc_info:
            Settings()
        assert "telegram_allowed_chat_ids" in str(exc_info.value)

    def test_telegram_already_a_list_passes_through(self) -> None:
        """Constructing Settings directly with a Python list (e.g. in a
        test or programmatically) must keep working unchanged."""
        s = Settings(telegram_allowed_chat_ids=[1, 2, 3])
        assert s.telegram_allowed_chat_ids == [1, 2, 3]

    # -- slack_allowed_channel_ids: list[str] -----------------------------

    def test_slack_empty_env_value_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_SLACK_ALLOWED_CHANNEL_IDS", "")
        s = Settings()
        assert s.slack_allowed_channel_ids == []

    def test_slack_plain_single_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_SLACK_ALLOWED_CHANNEL_IDS", "C123")
        s = Settings()
        assert s.slack_allowed_channel_ids == ["C123"]

    def test_slack_csv_value_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_SLACK_ALLOWED_CHANNEL_IDS", "C1, C2")
        s = Settings()
        assert s.slack_allowed_channel_ids == ["C1", "C2"]

    def test_slack_json_array_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_SLACK_ALLOWED_CHANNEL_IDS", '["C1", "C2"]')
        s = Settings()
        assert s.slack_allowed_channel_ids == ["C1", "C2"]

    # -- plugins_disabled: list[str] (CSV/empty/plain counterpart to the --
    # -- pre-existing TestPluginsDisabled JSON-only coverage above) -------

    def test_plugins_disabled_empty_env_value_is_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HIVEPILOT_PLUGINS_DISABLED", "")
        s = Settings()
        assert s.plugins_disabled == []

    def test_plugins_disabled_csv_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_PLUGINS_DISABLED", "rtk,obsidian")
        s = Settings()
        assert s.plugins_disabled == ["rtk", "obsidian"]

    # -- discovery_roots: list[str] with a non-empty default --------------

    def test_discovery_roots_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_DISCOVERY_ROOTS", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.discovery_roots == ["~/dev"]

    def test_discovery_roots_csv_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_DISCOVERY_ROOTS", "~/dev,~/work")
        s = Settings()
        assert s.discovery_roots == ["~/dev", "~/work"]

    # -- discord (list[int]) + signal (list[str]) spot checks -------------

    def test_discord_allowed_guild_ids_plain_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_DISCORD_ALLOWED_GUILD_IDS", "111,222")
        s = Settings()
        assert s.discord_allowed_guild_ids == [111, 222]

    def test_signal_allowed_numbers_plain_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_SIGNAL_ALLOWED_NUMBERS", "+15551234567")
        s = Settings()
        assert s.signal_allowed_numbers == ["+15551234567"]


class TestConciergeConfig:
    """Natural-language concierge (opt-in, default off) config surface."""

    def test_concierge_enabled_defaults_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_CHATOPS_CONCIERGE_ENABLED", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.chatops_concierge_enabled is False

    def test_concierge_enabled_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_CHATOPS_CONCIERGE_ENABLED", "true")
        s = Settings()
        assert s.chatops_concierge_enabled is True

    def test_default_role_defaults_to_ceo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_CHATOPS_DEFAULT_ROLE", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.chatops_default_role == "ceo"

    def test_default_role_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_CHATOPS_DEFAULT_ROLE", "cto")
        s = Settings()
        assert s.chatops_default_role == "cto"

    def test_concierge_model_defaults_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HIVEPILOT_CHATOPS_CONCIERGE_MODEL", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.chatops_concierge_model is None

    def test_concierge_model_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HIVEPILOT_CHATOPS_CONCIERGE_MODEL", "haiku")
        s = Settings()
        assert s.chatops_concierge_model == "haiku"
