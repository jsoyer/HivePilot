"""
Tests for YAML-backed role loading (roles.yaml → load_roles()).

Covers:
- load_roles() parses the active roles.yaml and produces the full 8-role
  "company" roster it defines (Sprint 2, roles-model-effort-config-owned
  PRD: the active roles.yaml -- whether the repo-root one or a real
  deployment's XDG-config override -- is an EXISTING, unchanged config that
  must keep working; it is no longer required to mirror the code-owned
  `_DEFAULT_ROLES` fallback, which Sprint 2 reduced to just `developer`).
- Missing roles.yaml triggers graceful fallback to the (now reduced)
  _DEFAULT_ROLES.
- prompt_file Path is resolved correctly for every loaded role.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

# The full "company" roster every currently-active roles.yaml on this
# project defines (repo-root roles.yaml, or a real deployment's XDG
# override) -- independent of the code-owned _DEFAULT_ROLES fallback.
EXPECTED_ROLE_NAMES = {
    "ceo",
    "chief_of_staff",
    "cto",
    "developer",
    "reviewer",
    "ciso",
    "qa",
    "documentation",
}


class TestLoadRolesFromYaml:
    """load_roles() with the real roles.yaml must produce the full roster.

    Sprint 2 note: previously this compared `load_roles()` 1:1 against
    `_DEFAULT_ROLES` (both were, by design, kept identical). Sprint 2
    deliberately decouples them -- `_DEFAULT_ROLES` is now a minimal
    generic-only fallback, while an existing, unchanged roles.yaml keeps
    shipping the full business roster. These tests now assert against the
    roster directly (mirroring tests/test_roles.py's EXPECTED_ROLE_NAMES)
    instead of `_DEFAULT_ROLES`.
    """

    def test_load_roles_from_yaml_has_expected_roster(self):
        from hivepilot.roles import load_roles

        loaded = load_roles()

        assert set(loaded.keys()) == EXPECTED_ROLE_NAMES, (
            f"Loaded role keys {set(loaded.keys())} != expected {EXPECTED_ROLE_NAMES}"
        )

        dev = loaded["developer"]
        assert dev.name == "developer"
        assert dev.title == "Developer"
        assert dev.model_profile == "coding"
        assert dev.runner == "claude"
        assert dev.permission_mode == "bypassPermissions"

    def test_load_roles_returns_eight_roles(self):
        from hivepilot.roles import load_roles

        loaded = load_roles()
        assert len(loaded) == 8

    def test_load_roles_inputs_outputs_are_non_trivial(self):
        from hivepilot.roles import load_roles

        loaded = load_roles()
        for name in EXPECTED_ROLE_NAMES:
            role = loaded[name]
            assert isinstance(role.inputs, list)
            assert isinstance(role.outputs, list)


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
        # Sprint 2 (roles-model-effort-config-owned PRD): the code-owned
        # fallback was reduced from the full 8-role roster to a single
        # generic `developer` role. Old expectation: `len(result) == 8`.
        assert len(result) == 1
        assert set(result) == {"developer"}


class TestPromptFileResolution:
    """Resolved prompt_file paths must be correct for every loaded role."""

    def test_prompt_file_resolves_under_prompts_dir(self):
        from hivepilot.roles import _PROMPTS_DIR, load_roles

        loaded = load_roles()
        for name, loaded_role in loaded.items():
            assert loaded_role.prompt_file == _PROMPTS_DIR / f"{name}.md", (
                f"Role '{name}': prompt_file mismatch, got {loaded_role.prompt_file}"
            )

    def test_prompt_files_are_absolute_paths(self):
        from hivepilot.roles import load_roles

        loaded = load_roles()
        for name, role in loaded.items():
            assert role.prompt_file.is_absolute(), (
                f"Role '{name}': prompt_file should be absolute, got {role.prompt_file}"
            )

    def test_prompt_files_contain_agents_subdir(self):
        from hivepilot.roles import load_roles

        loaded = load_roles()
        for name, role in loaded.items():
            assert "agents" in role.prompt_file.parts, (
                f"Role '{name}': prompt_file should be under prompts/agents/, "
                f"got {role.prompt_file}"
            )
