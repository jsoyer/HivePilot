"""
Integration tests verifying that examples/noxys/ is a complete, loadable
HivePilot config when HIVEPILOT_CONFIG_REPO=examples/noxys.

These tests exercise the full config loading chain to confirm the noxys
deployment config moved to examples/noxys/ works end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Absolute path to the noxys example config dir (relative to repo root).
_REPO_ROOT = Path(__file__).parent.parent
_NOXYS_DIR = _REPO_ROOT / "examples" / "noxys"


def _noxys_settings():
    """Return a Settings instance pointed at examples/noxys/."""
    from hivepilot.config import Settings

    return Settings(config_repo=str(_NOXYS_DIR))


class TestNoxysConfigDirectory:
    """examples/noxys/ must contain all required config files."""

    @pytest.mark.parametrize(
        "filename",
        [
            "roles.yaml",
            "projects.yaml",
            "policies.yaml",
            "groups.yaml",
            "pipelines.yaml",
            "tasks.yaml",
            "schedules.yaml",
            "model_profiles.yaml",
        ],
    )
    def test_required_yaml_present(self, filename: str):
        assert (_NOXYS_DIR / filename).exists(), (
            f"examples/noxys/{filename} is missing — git mv may not have completed"
        )

    def test_prompts_agents_dir_present(self):
        agents_dir = _NOXYS_DIR / "prompts" / "agents"
        assert agents_dir.is_dir(), "examples/noxys/prompts/agents/ is missing"

    @pytest.mark.parametrize(
        "prompt_file",
        [
            "ceo.md",
            "chief_of_staff.md",
            "cto.md",
            "developer.md",
            "reviewer.md",
            "ciso.md",
            "qa.md",
            "documentation.md",
        ],
    )
    def test_agent_prompt_present(self, prompt_file: str):
        path = _NOXYS_DIR / "prompts" / "agents" / prompt_file
        assert path.exists(), f"examples/noxys/prompts/agents/{prompt_file} is missing"


class TestNoxysRolesLoad:
    """load_roles() via HIVEPILOT_CONFIG_REPO=examples/noxys must succeed."""

    def test_roles_load_from_noxys_config(self):
        import hivepilot.config as config_module
        from hivepilot.roles import load_roles

        original = config_module.settings
        try:
            config_module.settings = _noxys_settings()
            loaded = load_roles()
        finally:
            config_module.settings = original

        assert len(loaded) >= 8, f"Expected at least 8 roles, got {len(loaded)}"

    def test_roles_prompt_files_exist(self):
        import hivepilot.config as config_module
        from hivepilot.roles import load_roles

        original = config_module.settings
        try:
            config_module.settings = _noxys_settings()
            loaded = load_roles()
        finally:
            config_module.settings = original

        for name, role in loaded.items():
            assert role.prompt_file.exists(), (
                f"Role '{name}': prompt_file does not exist: {role.prompt_file}"
            )

    def test_roles_prompt_files_under_noxys(self):
        """Confirm prompt_file paths are under examples/noxys/ (not the old package path)."""
        import hivepilot.config as config_module
        from hivepilot.roles import load_roles

        original = config_module.settings
        try:
            config_module.settings = _noxys_settings()
            loaded = load_roles()
        finally:
            config_module.settings = original

        for name, role in loaded.items():
            assert str(_NOXYS_DIR) in str(role.prompt_file), (
                f"Role '{name}': prompt_file should be under examples/noxys/, "
                f"got {role.prompt_file}"
            )


class TestNoxysProjectsLoad:
    """load_projects() via HIVEPILOT_CONFIG_REPO=examples/noxys must succeed."""

    def test_projects_load_from_noxys_config(self):
        from unittest.mock import patch

        import hivepilot.services.project_service as ps_module
        from hivepilot.services.project_service import load_projects

        noxys_settings = _noxys_settings()
        with patch.object(ps_module, "settings", noxys_settings):
            projects = load_projects()

        assert len(projects.projects) > 0, (
            "Expected at least one project in examples/noxys/projects.yaml"
        )


class TestNoxysProfilesLoad:
    """load_claude_profiles() via HIVEPILOT_CONFIG_REPO=examples/noxys must succeed."""

    def test_profiles_load_from_noxys_config(self):
        import hivepilot.config as config_module
        import hivepilot.services.profile_service as ps_module
        from hivepilot.services.profile_service import load_claude_profiles

        original = config_module.settings
        ps_module._cache.clear()
        try:
            config_module.settings = _noxys_settings()
            profiles = load_claude_profiles()
        finally:
            config_module.settings = original
            ps_module._cache.clear()

        # model_profiles.yaml may be empty or have entries — just confirm it loads
        assert isinstance(profiles, dict), "Expected profiles to be a dict"
