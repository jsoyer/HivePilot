"""HP-19: selectable OSS preset columns + roster overlay.

`model_profiles.yaml` grows `opencode:` / `openai:` columns beside grok/cursor.
`roster-presets/oss.yaml` is opt-in via `HIVEPILOT_ROSTER_PRESET=oss`. Default
roles.yaml vendors must stay put.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hivepilot.services import profile_service
from hivepilot.services.roster_preset import (
    clear_roster_preset_cache,
    load_roster_preset,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profiles():
    profile_service._cache.clear()
    try:
        yield profile_service.load_model_profiles(path=REPO_ROOT / "model_profiles.yaml")
    finally:
        profile_service._cache.clear()


def test_coding_architecture_automation_have_opencode_and_openai_columns(profiles) -> None:
    assert profiles["coding"]["opencode"] == "kimi-k2.7-code"
    assert profiles["coding"]["openai"] == "kimi-k2.7-code"
    assert profiles["architecture"]["opencode"] == "deepseek-v4-pro"
    assert profiles["architecture"]["openai"] == "deepseek-v4-pro"
    assert profiles["automation"]["opencode"] == "glm-5.3-flash"
    assert profiles["automation"]["openai"] == "glm-5.3-flash"


def test_oss_columns_do_not_replace_existing_vendor_columns(profiles) -> None:
    """HP-19 must not silently retarget grok/cursor/openrouter/claude."""
    coding = profiles["coding"]
    assert coding["model"] == "sonnet"
    assert coding["grok"] == "grok-4.6"
    assert coding["cursor"] == "gpt-5.3-codex-high"
    assert coding["openrouter"] == "nousresearch/hermes-4-70b"


def test_resolve_profile_model_opencode_and_openai(monkeypatch) -> None:
    from hivepilot.config import Settings

    profile_service._cache.clear()

    def fake_resolve(self, filename):  # noqa: ANN001
        return REPO_ROOT / "model_profiles.yaml"

    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve)
    assert profile_service.resolve_profile_model("automation", "opencode") == "glm-5.3-flash"
    assert profile_service.resolve_profile_model("automation", "openai") == "glm-5.3-flash"
    assert profile_service.resolve_profile_model("coding", "opencode") == "kimi-k2.7-code"
    assert profile_service.resolve_profile_model("architecture", "openai") == "deepseek-v4-pro"


def test_oss_roster_preset_file_exists_and_maps_all_roles() -> None:
    path = REPO_ROOT / "roster-presets" / "oss.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    roles = raw["roles"]
    expected = {
        "ceo",
        "cto",
        "ciso",
        "developer",
        "reviewer",
        "qa",
        "chief_of_staff",
        "documentation",
    }
    assert set(roles) == expected
    for spec in roles.values():
        assert spec["runner"] == "opencode"


def test_oss_preset_loads_via_roster_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hivepilot.config import settings

    preset_dir = tmp_path / "roster-presets"
    preset_dir.mkdir()
    (preset_dir / "oss.yaml").write_text(
        (REPO_ROOT / "roster-presets" / "oss.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "roster_preset", "oss")
    clear_roster_preset_cache()
    try:
        preset = load_roster_preset()
    finally:
        clear_roster_preset_cache()
    assert preset.name == "oss"
    assert preset.override_for("developer")["runner"] == "opencode"
    assert preset.override_for("documentation")["runner"] == "opencode"
    # Overlay does not pin models — profile columns resolve per runner.
    assert "model" not in preset.override_for("developer")


def test_default_roles_yaml_vendors_unchanged() -> None:
    """Selecting the OSS preset must not rewrite the shipped Claude/mix roster."""
    raw = yaml.safe_load((REPO_ROOT / "roles.yaml").read_text(encoding="utf-8"))
    by_name = {role["name"]: role for role in raw["roles"]}
    assert by_name["developer"]["runner"] == "claude"
    assert by_name["reviewer"]["runner"] == "codex"
    assert by_name["qa"]["runner"] == "cursor"
    assert by_name["chief_of_staff"]["runner"] == "cursor"
    assert by_name["documentation"]["runner"] == "gemini"
