"""
Tests for YAML-backed role loading (roles.yaml → load_roles()).

Covers:
- load_roles() parses roles.yaml and produces roles matching _DEFAULT_ROLES
  when HIVEPILOT_CONFIG_REPO=examples/noxys is active
- Missing roles.yaml triggers graceful fallback to _DEFAULT_ROLES
- prompt_file Path is resolved via resolve_config_path (not _PROMPTS_DIR)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _mock_settings_for_noxys() -> object:
    """Return a mock settings object that resolves config from examples/noxys/."""
    from hivepilot.config import Settings

    return Settings(config_repo=str(Path(__file__).parent.parent / "examples" / "noxys"))


class TestLoadRolesFromYaml:
    """load_roles() with the real roles.yaml must match the built-in defaults."""

    def test_load_roles_from_yaml_matches_defaults(self):
        from hivepilot import roles as roles_module
        from hivepilot.roles import _DEFAULT_ROLES, load_roles

        noxys_settings = _mock_settings_for_noxys()
        with patch.object(roles_module, "load_roles", wraps=load_roles):
            import hivepilot.config as config_module

            original_settings = config_module.settings
            try:
                config_module.settings = noxys_settings
                loaded = load_roles()
            finally:
                config_module.settings = original_settings

        assert set(loaded.keys()) == set(_DEFAULT_ROLES.keys()), (
            f"Loaded role keys {set(loaded.keys())} != default keys {set(_DEFAULT_ROLES.keys())}"
        )

        for name, default_role in _DEFAULT_ROLES.items():
            loaded_role = loaded[name]
            assert loaded_role.name == default_role.name, f"{name}: name mismatch"
            assert loaded_role.title == default_role.title, f"{name}: title mismatch"
            assert loaded_role.display_name == default_role.display_name, (
                f"{name}: display_name mismatch"
            )
            assert loaded_role.model_profile == default_role.model_profile, (
                f"{name}: model_profile mismatch"
            )
            assert loaded_role.runner == default_role.runner, f"{name}: runner mismatch"
            assert loaded_role.model == default_role.model, f"{name}: model mismatch"
            assert loaded_role.models == default_role.models, f"{name}: models mismatch"
            assert loaded_role.can_block == default_role.can_block, f"{name}: can_block mismatch"
            assert loaded_role.order == default_role.order, f"{name}: order mismatch"
            assert loaded_role.permission_mode == default_role.permission_mode, (
                f"{name}: permission_mode mismatch"
            )

    def test_load_roles_returns_eight_roles(self):
        from hivepilot.roles import load_roles

        noxys_settings = _mock_settings_for_noxys()
        import hivepilot.config as config_module

        original_settings = config_module.settings
        try:
            config_module.settings = noxys_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        assert len(loaded) == 8

    def test_load_roles_inputs_outputs_match_defaults(self):
        from hivepilot.roles import _DEFAULT_ROLES, load_roles

        noxys_settings = _mock_settings_for_noxys()
        import hivepilot.config as config_module

        original_settings = config_module.settings
        try:
            config_module.settings = noxys_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        for name, default_role in _DEFAULT_ROLES.items():
            loaded_role = loaded[name]
            assert loaded_role.inputs == default_role.inputs, f"{name}: inputs mismatch"
            assert loaded_role.outputs == default_role.outputs, f"{name}: outputs mismatch"


class TestAbsentFileFallback:
    """When roles.yaml is missing, load_roles() must return _DEFAULT_ROLES."""

    def test_absent_file_falls_back_to_defaults(self, monkeypatch):
        from hivepilot import roles as roles_module
        from hivepilot.roles import _DEFAULT_ROLES, load_roles

        # Point resolve_config_path to a path that does not exist
        non_existent = Path("/tmp/does_not_exist_hivepilot_roles_xyz.yaml")

        mock_settings = type(
            "MockSettings",
            (),
            {
                "roles_file": non_existent,
                "resolve_config_path": lambda self, f: non_existent,
            },
        )()

        with patch.object(roles_module, "load_roles", wraps=load_roles):
            # Patch settings inside the roles module's load_roles call
            import hivepilot.config as config_module

            original_settings = config_module.settings
            try:
                config_module.settings = mock_settings
                result = load_roles()
            finally:
                config_module.settings = original_settings

        assert result == _DEFAULT_ROLES, "Fallback should return _DEFAULT_ROLES on missing file"

    def test_absent_file_does_not_raise(self, monkeypatch):
        import hivepilot.config as config_module
        from hivepilot.roles import load_roles

        non_existent = Path("/tmp/does_not_exist_hivepilot_roles_abc.yaml")
        mock_settings = type(
            "MockSettings",
            (),
            {
                "roles_file": non_existent,
                "resolve_config_path": lambda self, f: non_existent,
            },
        )()

        original_settings = config_module.settings
        try:
            config_module.settings = mock_settings
            result = load_roles()  # must not raise
        finally:
            config_module.settings = original_settings

        assert result is not None
        assert len(result) == 8


class TestPromptFileResolution:
    """Resolved prompt_file paths must be under the config_repo prompts/agents/ dir."""

    def test_prompt_file_resolves_via_config_repo(self):
        """YAML-loaded roles must resolve prompt_file via resolve_config_path (config_repo)."""
        from hivepilot.roles import load_roles

        noxys_settings = _mock_settings_for_noxys()
        import hivepilot.config as config_module

        original_settings = config_module.settings
        try:
            config_module.settings = noxys_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        noxys_prompts_dir = Path(__file__).parent.parent / "examples" / "noxys" / "prompts" / "agents"
        for name, role in loaded.items():
            assert role.prompt_file.parent == noxys_prompts_dir, (
                f"Role '{name}': prompt_file should be under examples/noxys/prompts/agents/, "
                f"got {role.prompt_file}"
            )

    def test_prompt_files_are_absolute_paths(self):
        from hivepilot.roles import load_roles

        noxys_settings = _mock_settings_for_noxys()
        import hivepilot.config as config_module

        original_settings = config_module.settings
        try:
            config_module.settings = noxys_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        for name, role in loaded.items():
            assert role.prompt_file.is_absolute(), (
                f"Role '{name}': prompt_file should be absolute, got {role.prompt_file}"
            )

    def test_prompt_files_contain_agents_subdir(self):
        from hivepilot.roles import load_roles

        noxys_settings = _mock_settings_for_noxys()
        import hivepilot.config as config_module

        original_settings = config_module.settings
        try:
            config_module.settings = noxys_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        for name, role in loaded.items():
            assert "agents" in role.prompt_file.parts, (
                f"Role '{name}': prompt_file should be under prompts/agents/, "
                f"got {role.prompt_file}"
            )
