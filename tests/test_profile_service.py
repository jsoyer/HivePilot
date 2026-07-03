"""Tests for hivepilot.services.profile_service.

Verifies that model_profiles.yaml is resolved via the XDG/config_repo-aware
`settings.resolve_config_path`, not the cwd-only `settings.resolve_path`, so
an external config repo override is honored.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.config import Settings
from hivepilot.services import profile_service


def test_load_claude_profiles_uses_resolve_config_path(tmp_path: Path, monkeypatch) -> None:
    """load_claude_profiles must resolve its file through resolve_config_path
    so an XDG/config-repo override wins over the cwd-relative path."""
    calls: list[Path | str] = []

    override_file = tmp_path / "model_profiles.yaml"
    override_file.write_text(
        yaml.safe_dump({"claude_profiles": {"default": {"model": "opus"}}}),
        encoding="utf-8",
    )

    def fake_resolve_config_path(self, filename):
        calls.append(filename)
        return override_file

    def fail_resolve_path(self, *a, **k):
        raise AssertionError("resolve_path should not be called")

    # Settings is a pydantic BaseSettings instance; instance attributes can't
    # be reassigned arbitrarily, so patch the methods on the class instead.
    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve_config_path)
    monkeypatch.setattr(Settings, "resolve_path", fail_resolve_path)

    profile_service._cache.clear()
    data = profile_service.load_claude_profiles()

    assert calls, "resolve_config_path was never called"
    assert data == {"default": {"model": "opus"}}
