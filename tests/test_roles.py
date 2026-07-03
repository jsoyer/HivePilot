"""
Sprint 1.3 — Role abstraction tests.

Covers:
- All 8 roles are present in ROLES registry
- Each role's prompt_file exists on disk and is non-empty
- Each role's model_profile is a valid claude_profiles key in model_profiles.yaml
- list_roles() returns roles ordered by their pipeline position (order field)
- get_role() returns the expected Role instance with correct fields
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent

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

# Pipeline order: CEO → Chief of Staff → CTO → Developer → Reviewer → CISO → QA (+ Documentation)
EXPECTED_ORDER = [
    "ceo",
    "chief_of_staff",
    "cto",
    "developer",
    "reviewer",
    "ciso",
    "qa",
    "documentation",
]


def _valid_claude_profiles() -> set[str]:
    cfg_path = REPO_ROOT / "model_profiles.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    return set(data.get("claude_profiles", {}).keys())


class TestRolesRegistry:
    """All 8 roles must be present in the ROLES dict."""

    def test_all_eight_roles_present(self):
        from hivepilot.roles import ROLES

        assert set(ROLES.keys()) == EXPECTED_ROLE_NAMES

    def test_role_names_are_lowercase_snake_case(self):
        from hivepilot.roles import ROLES

        for name in ROLES:
            assert name == name.lower(), f"Role name '{name}' must be lowercase"
            assert " " not in name, f"Role name '{name}' must use underscores, not spaces"


class TestRolePromptFiles:
    """Each role's prompt_file must exist and be non-empty."""

    def test_all_prompt_files_exist(self):
        from hivepilot.roles import ROLES

        for name, role in ROLES.items():
            assert role.prompt_file.exists(), (
                f"Role '{name}' prompt_file does not exist: {role.prompt_file}"
            )

    def test_all_prompt_files_non_empty(self):
        from hivepilot.roles import ROLES

        for name, role in ROLES.items():
            content = role.prompt_file.read_text().strip()
            assert len(content) > 0, f"Role '{name}' prompt_file is empty"

    def test_prompt_files_in_agents_subdir(self):
        from hivepilot.roles import ROLES

        for name, role in ROLES.items():
            assert "agents" in role.prompt_file.parts, (
                f"Role '{name}' prompt_file should be under prompts/agents/: {role.prompt_file}"
            )


class TestRoleModelProfiles:
    """Each role's model_profile must be a valid key in model_profiles.yaml claude_profiles."""

    def test_all_model_profiles_are_valid(self):
        from hivepilot.roles import ROLES

        valid_profiles = _valid_claude_profiles()
        for name, role in ROLES.items():
            assert role.model_profile in valid_profiles, (
                f"Role '{name}' has invalid model_profile '{role.model_profile}'. "
                f"Valid profiles: {valid_profiles}"
            )


class TestListRoles:
    """list_roles() must return roles in pipeline order."""

    def test_list_roles_returns_all_eight(self):
        from hivepilot.roles import list_roles

        roles = list_roles()
        assert len(roles) == 8

    def test_list_roles_pipeline_order(self):
        from hivepilot.roles import list_roles

        roles = list_roles()
        names = [r.name for r in roles]
        assert names == EXPECTED_ORDER, (
            f"list_roles() order mismatch.\nGot:      {names}\nExpected: {EXPECTED_ORDER}"
        )

    def test_list_roles_sorted_by_order_field(self):
        from hivepilot.roles import list_roles

        roles = list_roles()
        orders = [r.order for r in roles]
        assert orders == sorted(orders), "list_roles() must be sorted ascending by order field"


class TestGetRole:
    """get_role() must return the correct Role instance."""

    def test_get_role_developer_fields(self):
        from hivepilot.roles import get_role

        dev = get_role("developer")
        assert dev.name == "developer"
        assert dev.title == "Developer"
        assert dev.model_profile == "coding"
        assert "prompt" in dev.inputs or len(dev.inputs) > 0
        assert len(dev.outputs) > 0
        assert isinstance(dev.can_block, bool)
        assert isinstance(dev.order, int)

    def test_get_role_ceo_is_first(self):
        from hivepilot.roles import get_role

        ceo = get_role("ceo")
        assert ceo.order == 1

    def test_get_role_unknown_raises(self):
        from hivepilot.roles import get_role

        with pytest.raises(KeyError):
            get_role("nonexistent_role")

    def test_get_role_all_names(self):
        from hivepilot.roles import get_role

        for name in EXPECTED_ROLE_NAMES:
            role = get_role(name)
            assert role.name == name


class TestRoleModel:
    """Role Pydantic model must have all required fields."""

    def test_role_model_fields(self):
        from hivepilot.roles import Role

        fields = Role.model_fields
        required_fields = {
            "name",
            "title",
            "prompt_file",
            "model_profile",
            "inputs",
            "outputs",
            "can_block",
            "order",
        }
        assert required_fields.issubset(set(fields.keys())), (
            f"Role model missing fields: {required_fields - set(fields.keys())}"
        )

    def test_role_is_pydantic_model(self):
        from pydantic import BaseModel

        from hivepilot.roles import Role

        assert issubclass(Role, BaseModel)
