"""Tests for profile_service.load_claude_profiles().

Verifies that the loader uses settings.resolve_config_path (not resolve_path),
so that model_profiles.yaml resolves via the config_repo chain.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


class TestLoadClaudeProfilesResolution:
    """load_claude_profiles() must resolve the file via resolve_config_path."""

    def test_missing_profiles_file_returns_empty_dict(self, tmp_path):
        """When model_profiles.yaml does not exist, return an empty dict gracefully."""
        import hivepilot.services.profile_service as ps_module

        non_existent = tmp_path / "model_profiles.yaml"
        mock_settings = MagicMock()
        mock_settings.resolve_config_path.return_value = non_existent
        mock_settings.claude_profiles_file = Path("model_profiles.yaml")

        # Clear the module-level cache so our mock path is used
        ps_module._cache.clear()
        with patch.object(ps_module, "settings", mock_settings):
            result = ps_module.load_claude_profiles()

        assert result == {}
        mock_settings.resolve_config_path.assert_called_once()

    def test_profiles_loaded_from_yaml(self, tmp_path):
        """When model_profiles.yaml exists, profiles dict is returned."""
        import yaml

        import hivepilot.services.profile_service as ps_module

        profiles_file = tmp_path / "model_profiles.yaml"
        profiles_file.write_text(
            yaml.dump(
                {
                    "claude_profiles": {
                        "coding": {"model": "claude-3-5-sonnet-20241022"},
                        "architecture": {"model": "claude-opus-4-5"},
                    }
                }
            ),
            encoding="utf-8",
        )

        mock_settings = MagicMock()
        mock_settings.resolve_config_path.return_value = profiles_file
        mock_settings.claude_profiles_file = Path("model_profiles.yaml")

        ps_module._cache.clear()
        with patch.object(ps_module, "settings", mock_settings):
            result = ps_module.load_claude_profiles()

        assert "coding" in result
        assert "architecture" in result
        assert result["coding"]["model"] == "claude-3-5-sonnet-20241022"

    def test_resolve_config_path_is_called_not_resolve_path(self, tmp_path):
        """Ensure resolve_config_path (not resolve_path) is the method invoked."""
        import hivepilot.services.profile_service as ps_module

        mock_settings = MagicMock()
        non_existent = tmp_path / "model_profiles.yaml"
        mock_settings.resolve_config_path.return_value = non_existent
        mock_settings.claude_profiles_file = Path("model_profiles.yaml")

        ps_module._cache.clear()
        with patch.object(ps_module, "settings", mock_settings):
            ps_module.load_claude_profiles()

        mock_settings.resolve_config_path.assert_called_once()
        mock_settings.resolve_path.assert_not_called()
