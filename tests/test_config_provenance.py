"""Unit tests for hivepilot.services.config_provenance (Sprint 2 of the
config-edit-commands PRD).

Covers the Provenance contract, secret redaction, and the XDG -> config_repo
-> base_dir rank walk that `hivepilot config get` / `config list` build on.
CLI-level behavior (argument parsing, table rendering) is covered separately
in tests/test_cli_config_get.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.config import Settings
from hivepilot.services.config_provenance import (
    REDACTED,
    Provenance,
    all_keys,
    clear_secret_values,
    is_secret_field,
    redact_text,
    register_secret_value,
    registered_secret_values,
    resolve_with_provenance,
)


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    """Build a Settings instance isolated from the real machine's XDG dirs
    and .env file, so provenance rank assertions are deterministic."""
    xdg_root = tmp_path / "xdg-home"
    xdg_root.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
    kwargs: dict[str, object] = {"base_dir": tmp_path, "_env_file": None}
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type, call-arg]


class TestProvenanceDataclass:
    def test_is_frozen(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _settings(tmp_path, monkeypatch)
        prov = resolve_with_provenance("concurrency_limit", cfg=cfg)
        assert isinstance(prov, Provenance)
        with pytest.raises(Exception):
            prov.value = 99  # type: ignore[misc]

    def test_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _settings(tmp_path, monkeypatch)
        prov = resolve_with_provenance("concurrency_limit", cfg=cfg)
        assert hasattr(prov, "value")
        assert hasattr(prov, "source_path")
        assert hasattr(prov, "xdg_rank")
        assert hasattr(prov, "redacted")


class TestNonPathSettings:
    def test_plain_setting_has_rank_zero_and_no_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _settings(tmp_path, monkeypatch)
        prov = resolve_with_provenance("concurrency_limit", cfg=cfg)
        assert prov.value == cfg.concurrency_limit
        assert prov.source_path is None
        assert prov.xdg_rank == 0
        assert prov.redacted is False


class TestSecretRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "chatops_token",
            "telegram_bot_token",
            "telegram_webhook_secret",
            "worker_token",
            "vault_token",
            "slack_bot_token",
            "slack_signing_secret",
            "discord_bot_token",
            "linear_api_key",
            "notion_token",
            "event_webhook_token",
            "database_url",
            "redis_url",
        ],
    )
    def test_secret_fields_detected(self, key: str) -> None:
        assert is_secret_field(key) is True

    @pytest.mark.parametrize(
        "key",
        ["concurrency_limit", "default_runner", "claude_command", "base_dir", "output_format"],
    )
    def test_non_secret_fields_not_flagged(self, key: str) -> None:
        assert is_secret_field(key) is False

    def test_redacted_value_never_leaks_raw_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _settings(tmp_path, monkeypatch, chatops_token="super-secret-raw-value")
        prov = resolve_with_provenance("chatops_token", cfg=cfg)
        assert prov.redacted is True
        assert prov.value == "REDACTED"
        assert "super-secret-raw-value" not in str(prov.value)


class TestUnknownKey:
    def test_unknown_key_raises_key_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _settings(tmp_path, monkeypatch)
        with pytest.raises(KeyError):
            resolve_with_provenance("not_a_real_setting", cfg=cfg)


class TestXdgRankWalk:
    def test_xdg_override_reports_rank_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _settings(tmp_path, monkeypatch)
        xdg_hivepilot = cfg.xdg_config_home
        xdg_hivepilot.mkdir(parents=True, exist_ok=True)
        (xdg_hivepilot / "projects.yaml").write_text("projects: {}\n", encoding="utf-8")

        prov = resolve_with_provenance("projects_file", cfg=cfg)
        assert prov.xdg_rank == 1
        assert prov.source_path == xdg_hivepilot / "projects.yaml"

    def test_config_repo_override_reports_rank_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_dir = tmp_path / "config-repo"
        repo_dir.mkdir()
        (repo_dir / "groups.yaml").write_text("groups: {}\n", encoding="utf-8")

        cfg = _settings(tmp_path, monkeypatch, config_repo=str(repo_dir))

        prov = resolve_with_provenance("groups_file", cfg=cfg)
        assert prov.xdg_rank == 2
        assert prov.source_path == repo_dir / "groups.yaml"

    def test_base_dir_fallback_reports_rank_three(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _settings(tmp_path, monkeypatch)
        prov = resolve_with_provenance("tasks_file", cfg=cfg)
        assert prov.xdg_rank == 3
        assert prov.source_path == tmp_path / "tasks.yaml"

    def test_non_file_backed_path_setting_is_not_walked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """base_dir itself is a Path setting but not a `_file`-suffixed
        config file resolved through the XDG chain — must stay rank 0."""
        cfg = _settings(tmp_path, monkeypatch)
        prov = resolve_with_provenance("base_dir", cfg=cfg)
        assert prov.xdg_rank == 0
        assert prov.source_path is None


class TestAllKeys:
    def test_all_keys_matches_settings_model_fields(self) -> None:
        keys = all_keys()
        assert set(keys) == set(Settings.model_fields.keys())

    def test_all_keys_is_non_empty_list(self) -> None:
        assert isinstance(all_keys(), list)
        assert len(all_keys()) > 0


class TestSecretValueRegistry:
    def setup_method(self) -> None:
        clear_secret_values()

    def teardown_method(self) -> None:
        clear_secret_values()

    def test_register_then_redact(self) -> None:
        register_secret_value("a-long-secret-value")
        assert redact_text("x a-long-secret-value y") == f"x {REDACTED} y"

    def test_registered_values_snapshot(self) -> None:
        register_secret_value("another-long-secret")
        assert "another-long-secret" in registered_secret_values()

    def test_short_values_ignored(self) -> None:
        register_secret_value("ab")
        assert registered_secret_values() == frozenset()

    def test_non_string_ignored(self) -> None:
        register_secret_value(None)  # type: ignore[arg-type]
        assert registered_secret_values() == frozenset()

    def test_redact_noop_without_registration(self) -> None:
        assert redact_text("nothing to hide") == "nothing to hide"

    def test_longer_value_redacted_before_shorter_overlap(self) -> None:
        register_secret_value("secretvalue")
        register_secret_value("secretvalue-extended")
        out = redact_text("secretvalue-extended")
        # The longer value wins; no partial 'secretvalue' left dangling text.
        assert out == REDACTED
