"""Sprint 4: model_profiles.yaml dedup + loader guard.

Verifies:
1. `config/model_profiles.yaml` (dead — not on the resolve_config_path chain,
   documentary-only mirror of hivepilot/roles.py) no longer exists on disk.
2. `load_claude_profiles()` still returns the exact pre-change root data
   (snapshot captured before deletion in this sprint).
3. If a stray `config/model_profiles.yaml` reappears next to the resolved
   file, `load_claude_profiles` emits a guard warning but still returns the
   root-based data — it must never silently read the stray copy.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from hivepilot.config import Settings
from hivepilot.services import profile_service

REPO_ROOT = Path(__file__).resolve().parents[1]

# Snapshot of hivepilot's root model_profiles.yaml `claude_profiles` key,
# captured before config/model_profiles.yaml was deleted in this sprint.
# config/model_profiles.yaml used an unrelated schema (`roles`/`fallback`
# runner bindings, documented as a non-authoritative mirror of
# hivepilot/roles.py) and had no `claude_profiles` key, so nothing needed
# to be merged into this snapshot.
PRE_CHANGE_SNAPSHOT = {
    "coding": {"model": "sonnet"},
    "architecture": {"model": "opus"},
    "automation": {"model": "haiku"},
}


def test_config_model_profiles_no_longer_exists() -> None:
    """The dead config/model_profiles.yaml copy must be gone."""
    assert not (REPO_ROOT / "config" / "model_profiles.yaml").exists()


def test_root_model_profiles_still_exists() -> None:
    """The single source of truth (repo root) must remain."""
    assert (REPO_ROOT / "model_profiles.yaml").exists()


def test_load_claude_profiles_matches_pre_change_snapshot() -> None:
    """load_claude_profiles() must still return (at least) the pre-change data."""
    profile_service._cache.clear()
    # Absolute path resolves to itself in resolve_config_path (xdg / abs_path
    # collapses to abs_path), so this reads the real repo-root file directly
    # regardless of cwd or any XDG/config_repo override active in the env.
    data = profile_service.load_claude_profiles(path=REPO_ROOT / "model_profiles.yaml")
    for key, value in PRE_CHANGE_SNAPSHOT.items():
        assert data.get(key) == value, f"missing/changed profile: {key}"


def test_stray_config_copy_triggers_guard_warning_and_is_ignored(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A resurrected config/model_profiles.yaml must never be silently read."""
    root_file = tmp_path / "model_profiles.yaml"
    root_file.write_text(
        yaml.safe_dump({"claude_profiles": {"default": {"model": "root-value"}}}),
        encoding="utf-8",
    )

    stray_dir = tmp_path / "config"
    stray_dir.mkdir()
    stray_file = stray_dir / "model_profiles.yaml"
    stray_file.write_text(
        yaml.safe_dump({"claude_profiles": {"default": {"model": "stray-value"}}}),
        encoding="utf-8",
    )

    def fake_resolve_config_path(self, filename):
        return root_file

    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve_config_path)

    profile_service._cache.clear()
    with caplog.at_level(logging.WARNING, logger="hivepilot.services.profile_service"):
        data = profile_service.load_claude_profiles()

    assert data == {"default": {"model": "root-value"}}, "must return root data, not stray"
    assert any(
        "stray" in record.message.lower() and "config/model_profiles.yaml" in record.message
        for record in caplog.records
    ), "expected a guard warning about the stray config/model_profiles.yaml copy"


def test_no_stray_copy_emits_no_guard_warning(tmp_path: Path, monkeypatch, caplog) -> None:
    """No warning should be emitted when there is no stray copy to ignore."""
    root_file = tmp_path / "model_profiles.yaml"
    root_file.write_text(
        yaml.safe_dump({"claude_profiles": {"default": {"model": "root-value"}}}),
        encoding="utf-8",
    )

    def fake_resolve_config_path(self, filename):
        return root_file

    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve_config_path)

    profile_service._cache.clear()
    with caplog.at_level(logging.WARNING, logger="hivepilot.services.profile_service"):
        profile_service.load_claude_profiles()

    assert not any("stray" in record.message.lower() for record in caplog.records)
