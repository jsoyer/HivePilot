"""Tests for hivepilot.utils.startup_paths — the bug-debt fix for a long-
finished-but-never-shipped item: every long-running HivePilot entry point
(api server, scheduler daemon) must log ONCE, at INFO, the RESOLVED ABSOLUTE
paths it is actually using for its cwd-sensitive files (state DB, Telegram
stream-topics registry, config dir, prompts dir, Obsidian vault) — a service
started with `cwd=/` (OpenRC/systemd default) resolves every relative-by-
default `Settings` path field to a COMPLETELY DIFFERENT location than a CLI
run from `$HOME`, silently, and that split has cost real debugging hours.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hivepilot.config import Settings
from hivepilot.utils.startup_paths import log_resolved_startup_paths


def _rendered(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        [r.getMessage() for r in caplog.records] + [str(r.msg) for r in caplog.records]
    )


class TestLogResolvedStartupPaths:
    def test_logs_all_five_resolved_absolute_paths(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = Settings(base_dir=tmp_path)
        with caplog.at_level(logging.INFO):
            log_resolved_startup_paths(settings)

        rendered = _rendered(caplog)
        assert "startup.resolved_paths" in rendered

        expected_state_db = str((tmp_path / settings.state_db).expanduser().resolve())
        expected_prompts_dir = str((tmp_path / settings.prompts_dir).expanduser().resolve())
        expected_vault = str((tmp_path / settings.obsidian_vault).expanduser().resolve())
        expected_config_dir = str(settings.xdg_config_home)

        assert expected_state_db in rendered
        assert expected_prompts_dir in rendered
        assert expected_vault in rendered
        assert expected_config_dir in rendered
        assert "stream_topics.json" in rendered  # default topics registry filename

        # Every path logged must be absolute — the whole point of this fix.
        for value in (expected_state_db, expected_prompts_dir, expected_vault, expected_config_dir):
            assert Path(value).is_absolute()

    def test_honors_stream_topics_registry_path_override(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        override = tmp_path / "custom" / "topics.json"
        settings = Settings(base_dir=tmp_path, stream_topics_registry_path=override)
        with caplog.at_level(logging.INFO):
            log_resolved_startup_paths(settings)

        rendered = _rendered(caplog)
        assert str(override.expanduser().resolve()) in rendered

    def test_idempotent_per_settings_instance_logs_only_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = Settings(base_dir=tmp_path)
        with caplog.at_level(logging.INFO):
            log_resolved_startup_paths(settings)
            log_resolved_startup_paths(settings)  # same instance -- must not spam

        matches = [r for r in caplog.records if "startup.resolved_paths" in r.getMessage()]
        assert len(matches) == 1

    def test_different_settings_instances_each_get_their_own_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings_a = Settings(base_dir=tmp_path / "a")
        settings_b = Settings(base_dir=tmp_path / "b")
        with caplog.at_level(logging.INFO):
            log_resolved_startup_paths(settings_a)
            log_resolved_startup_paths(settings_b)

        matches = [r for r in caplog.records if "startup.resolved_paths" in r.getMessage()]
        assert len(matches) == 2
