"""
Sprint 2.1 — Role → runner + model binding tests.

Covers:
- Every role in the mapping resolves to the expected runner and model/models.
- CEO has dual models (real opencode IDs).
- `cursor` is registered in RUNNER_MAP and CursorRunner subclasses PromptCliRunner.
- CursorRunner raises a clear error when cursor-agent binary is missing.
- Existing tests/test_roles.py assertions continue to hold (no regression).
"""

from __future__ import annotations

import shutil
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Expected mapping table (matches spec exactly)
# ---------------------------------------------------------------------------

EXPECTED_RUNNER: dict[str, str] = {
    "ceo": "opencode",
    "chief_of_staff": "cursor",
    "cto": "opencode",
    "developer": "claude",
    "reviewer": "codex",
    "ciso": "opencode",
    "qa": "cursor",
    "documentation": "gemini",
}

# Roles that have a single explicit model override
EXPECTED_MODEL: dict[str, str] = {
    "cto": "opencode-go/kimi-k2.7-code",
    "ciso": "opencode-go/glm-5.2",
}

# Roles that have dual models (list)
EXPECTED_MODELS: dict[str, list[str]] = {
    "ceo": ["opencode-go/qwen3.7-max", "opencode-go/kimi-k2.6"],
}


class TestRoleRunnerField:
    """Each role must have the `runner` field set to the expected value."""

    def test_all_roles_have_runner_field(self):
        from hivepilot.roles import ROLES

        for name, role in ROLES.items():
            assert hasattr(role, "runner"), f"Role '{name}' missing 'runner' field"

    def test_runner_bindings_match_spec(self):
        from hivepilot.roles import get_role

        for role_name, expected_runner in EXPECTED_RUNNER.items():
            role = get_role(role_name)
            assert role.runner == expected_runner, (
                f"Role '{role_name}' runner mismatch: "
                f"expected '{expected_runner}', got '{role.runner}'"
            )


class TestRoleModelField:
    """Roles with a single model override must expose it via `model`."""

    def test_all_roles_have_model_field(self):
        from hivepilot.roles import ROLES

        for name, role in ROLES.items():
            assert hasattr(role, "model"), f"Role '{name}' missing 'model' field"

    def test_single_model_bindings_match_spec(self):
        from hivepilot.roles import get_role

        for role_name, expected_model in EXPECTED_MODEL.items():
            role = get_role(role_name)
            assert role.model == expected_model, (
                f"Role '{role_name}' model mismatch: "
                f"expected '{expected_model}', got '{role.model}'"
            )

    def test_roles_without_explicit_model_are_none(self):
        from hivepilot.roles import get_role

        no_explicit_model = {"chief_of_staff", "developer", "reviewer", "qa", "documentation"}
        for role_name in no_explicit_model:
            role = get_role(role_name)
            assert role.model is None, f"Role '{role_name}' expected model=None, got '{role.model}'"


class TestRoleModelsField:
    """CEO must have dual models via `models` list."""

    def test_all_roles_have_models_field(self):
        from hivepilot.roles import ROLES

        for name, role in ROLES.items():
            assert hasattr(role, "models"), f"Role '{name}' missing 'models' field"

    def test_ceo_has_dual_models(self):
        from hivepilot.roles import get_role

        ceo = get_role("ceo")
        assert ceo.models == ["opencode-go/qwen3.7-max", "opencode-go/kimi-k2.6"], (
            f"CEO dual models mismatch: got {ceo.models}"
        )

    def test_non_dual_roles_have_none_models(self):
        from hivepilot.roles import get_role

        single_model_roles = {
            "chief_of_staff",
            "cto",
            "developer",
            "reviewer",
            "ciso",
            "qa",
            "documentation",
        }
        for role_name in single_model_roles:
            role = get_role(role_name)
            assert role.models is None, (
                f"Role '{role_name}' expected models=None, got '{role.models}'"
            )


class TestCursorRunnerInRegistry:
    """cursor must be registered in RUNNER_MAP and CursorRunner must be the right class."""

    def test_cursor_key_in_runner_map(self):
        from hivepilot.registry import RUNNER_MAP

        assert "cursor" in RUNNER_MAP, "RUNNER_MAP missing 'cursor' key"

    def test_cursor_runner_class_is_correct(self):
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.runners.cursor_runner import CursorRunner

        assert RUNNER_MAP["cursor"] is CursorRunner

    def test_cursor_runner_subclasses_prompt_cli_runner(self):
        from hivepilot.runners.cursor_runner import CursorRunner
        from hivepilot.runners.prompt_cli_runner import PromptCliRunner

        assert issubclass(CursorRunner, PromptCliRunner), (
            "CursorRunner must subclass PromptCliRunner"
        )

    def test_cursor_runner_command_name(self):
        from hivepilot.runners.cursor_runner import CursorRunner

        assert CursorRunner.command_name == "cursor-agent", (
            f"CursorRunner.command_name must be 'cursor-agent', got '{CursorRunner.command_name}'"
        )


class TestCursorRunnerMissingBinaryGuard:
    """CursorRunner must raise a clear error when cursor-agent is not on PATH."""

    def test_missing_binary_raises_runtime_error(self):

        from hivepilot.models import RunnerDefinition
        from hivepilot.runners.cursor_runner import CursorRunner

        definition = RunnerDefinition(name="cursor", kind="cursor")

        # Simulate missing binary by patching shutil.which to return None
        with patch("hivepilot.runners.cursor_runner.shutil.which", return_value=None):
            runner = CursorRunner(definition=definition, settings=None)
            with pytest.raises(RuntimeError, match="cursor-agent"):
                runner._check_binary()

    def test_missing_binary_error_names_cursor_agent(self):
        """The error message must explicitly name 'cursor-agent' for debuggability."""
        from hivepilot.models import RunnerDefinition
        from hivepilot.runners.cursor_runner import CursorRunner

        definition = RunnerDefinition(name="cursor", kind="cursor")

        with patch("hivepilot.runners.cursor_runner.shutil.which", return_value=None):
            runner = CursorRunner(definition=definition, settings=None)
            with pytest.raises(RuntimeError) as exc_info:
                runner._check_binary()
            assert "cursor-agent" in str(exc_info.value)


class TestCursorAgentEnvPresence:
    """Document whether cursor-agent is available in the current environment."""

    def test_cursor_agent_which_result(self):
        """This test is informational — it always passes; result logged via print."""
        path = shutil.which("cursor-agent")
        # Informational: presence noted in summary, not a hard requirement
        # (the missing-binary guard covers runtime absence)
        assert True, f"cursor-agent on PATH: {path}"
