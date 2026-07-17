"""Sprint 2 (roles-model-effort-config-owned PRD): roles config-owned +
fail-closed validation.

Covers:
- `_DEFAULT_ROLES` is reduced to exactly {"developer"} -- no hard-coded model,
  no opencode/gemini dependency.
- `examples/roles.yaml` ships (NOT auto-loaded), and every entry in it
  restores as a valid `Role(**entry)` (loads through `load_roles()`'s own
  resolution logic when pointed at that file).
- Unknown `task.role` -> actionable ValueError at validation, never a bare
  KeyError at dispatch.
- `agent_rules.get_rules_for_role()` is safe for a role absent from
  ROLE_RULES/ROLES.
- `cli.py`'s `debate --role` default no longer assumes "ceo" exists.
- Existing roles.yaml-based configs (this repo's own root roles.yaml) keep
  working -- the full-replace loader is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_ROLES_YAML = REPO_ROOT / "examples" / "roles.yaml"

REMOVED_BUSINESS_ROLES = {
    "ceo",
    "chief_of_staff",
    "cto",
    "reviewer",
    "ciso",
    "qa",
    "documentation",
}


# ---------------------------------------------------------------------------
# Invariant 1 -- Single Generic Built-in Role
# ---------------------------------------------------------------------------


class TestDefaultRolesReducedToGenericDeveloper:
    def test_default_roles_keys_is_exactly_developer(self):
        from hivepilot.roles import _DEFAULT_ROLES

        assert set(_DEFAULT_ROLES) == {"developer"}

    def test_developer_runner_is_claude(self):
        from hivepilot.roles import _DEFAULT_ROLES

        assert _DEFAULT_ROLES["developer"].runner == "claude"

    def test_developer_has_no_hardcoded_model(self):
        from hivepilot.roles import _DEFAULT_ROLES

        dev = _DEFAULT_ROLES["developer"]
        assert dev.model is None
        assert dev.models is None

    def test_developer_keeps_permission_mode(self):
        from hivepilot.roles import _DEFAULT_ROLES

        assert _DEFAULT_ROLES["developer"].permission_mode == "bypassPermissions"

    def test_no_default_role_depends_on_optional_runner_plugins(self):
        """Invariant 2 -- no code-default role -> optional plugin (opencode/gemini)."""
        from hivepilot.roles import _DEFAULT_ROLES

        for name, role in _DEFAULT_ROLES.items():
            assert role.runner not in ("opencode", "gemini"), (
                f"Default role '{name}' must not depend on an optional runner plugin, "
                f"got runner={role.runner!r}"
            )

    def test_removed_business_roles_absent_from_defaults(self):
        from hivepilot.roles import _DEFAULT_ROLES

        for name in REMOVED_BUSINESS_ROLES:
            assert name not in _DEFAULT_ROLES

    def test_default_roles_fallback_returns_only_developer(self, monkeypatch):
        """load_roles() with a missing roles.yaml falls back to the (now
        reduced) _DEFAULT_ROLES."""
        from hivepilot import roles as roles_module

        non_existent = Path("/tmp/does_not_exist_hivepilot_roles_sprint2.yaml")
        mock_settings = type(
            "MockSettings",
            (),
            {
                "roles_file": non_existent,
                "resolve_config_path": lambda self, f: non_existent,
            },
        )()

        import hivepilot.config as config_module

        original_settings = config_module.settings
        try:
            config_module.settings = mock_settings
            result = roles_module.load_roles()
        finally:
            config_module.settings = original_settings

        assert set(result) == {"developer"}


# ---------------------------------------------------------------------------
# Invariant 3 -- Example roles.yaml Ships (restorable template, not auto-loaded)
# ---------------------------------------------------------------------------


class TestExampleRolesYamlShips:
    def test_example_file_exists(self):
        assert EXAMPLES_ROLES_YAML.exists(), (
            f"examples/roles.yaml must exist at {EXAMPLES_ROLES_YAML}"
        )

    def test_example_file_not_the_active_roles_file(self):
        """The example must live under examples/, never at the repo root
        (which is `settings.roles_file`'s cwd-fallback location) -- it must
        never be auto-loaded."""
        from hivepilot.config import settings

        assert EXAMPLES_ROLES_YAML != (REPO_ROOT / settings.roles_file)
        assert "examples" in EXAMPLES_ROLES_YAML.parts

    def test_example_defines_all_removed_business_roles(self):
        data = yaml.safe_load(EXAMPLES_ROLES_YAML.read_text(encoding="utf-8"))
        names = {entry["name"] for entry in data["roles"]}
        assert REMOVED_BUSINESS_ROLES.issubset(names), (
            f"examples/roles.yaml missing: {REMOVED_BUSINESS_ROLES - names}"
        )

    def test_every_example_entry_loads_as_a_valid_role(self):
        """Each entry must construct a valid Role(**entry) once prompt_file
        is resolved -- i.e. the example is genuinely restorable, not just
        parseable YAML."""
        from hivepilot.roles import _PROMPTS_DIR, Role

        data = yaml.safe_load(EXAMPLES_ROLES_YAML.read_text(encoding="utf-8"))
        for entry in data["roles"]:
            entry = dict(entry)
            prompt_filename = entry.pop("prompt_file")
            entry["prompt_file"] = _PROMPTS_DIR / prompt_filename
            role = Role(**entry)  # must not raise
            assert role.name == entry["name"]

    def test_example_roles_yaml_fully_restores_via_load_roles(self, monkeypatch, tmp_path):
        """Pointing the real `load_roles()` resolution chain at
        examples/roles.yaml (as if it were the active roles.yaml) must
        restore every removed business role with its original bindings.

        XDG_CONFIG_HOME is isolated to a scratch dir so a real
        ~/.config/hivepilot/roles.yaml on the host machine can't shadow the
        base_dir override under test (tier 1 of resolve_config_path always
        wins over tier 3 otherwise)."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        import hivepilot.config as config_module
        from hivepilot.roles import load_roles

        test_settings = config_module.Settings(base_dir=REPO_ROOT / "examples")
        original_settings = config_module.settings
        try:
            config_module.settings = test_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        assert REMOVED_BUSINESS_ROLES.issubset(set(loaded))
        assert loaded["ceo"].runner == "opencode"
        assert loaded["ceo"].models == ["opencode-go/qwen3.7-max", "opencode-go/kimi-k2.6"]
        assert loaded["reviewer"].runner == "codex"
        assert loaded["reviewer"].model == "gpt-5.5"
        assert loaded["documentation"].runner == "gemini"


# ---------------------------------------------------------------------------
# Invariant 4 -- Fail-Closed Role Validation
# ---------------------------------------------------------------------------


class TestFailClosedRoleValidation:
    def test_unknown_role_is_actionable_valueerror_not_keyerror(self):
        from hivepilot.models import TaskConfig, TasksFile
        from hivepilot.services.pipeline_service import validate_roles

        tasks = TasksFile(tasks={"task-a": TaskConfig(description="d", role="totally_unknown")})

        with pytest.raises(ValueError) as exc_info:
            validate_roles(tasks)

        assert not isinstance(exc_info.value, KeyError)
        message = str(exc_info.value)
        assert "task-a" in message
        assert "totally_unknown" in message
        assert "roles.yaml" in message

    def test_known_role_passes_validation(self):
        from hivepilot.models import TaskConfig, TasksFile
        from hivepilot.services.pipeline_service import validate_roles

        tasks = TasksFile(tasks={"task-a": TaskConfig(description="d", role="developer")})
        validate_roles(tasks)  # must not raise

    def test_validate_pipeline_end_to_end_actionable_error(self):
        from hivepilot.models import PipelineConfig, PipelineStage, TaskConfig, TasksFile
        from hivepilot.services.pipeline_service import validate_pipeline

        pipeline = PipelineConfig(
            description="t", stages=[PipelineStage(name="Stage A", task="task-a")]
        )
        tasks = TasksFile(tasks={"task-a": TaskConfig(description="d", role="ghost_role")})

        with pytest.raises(ValueError, match="ghost_role"):
            validate_pipeline(pipeline, tasks)


# ---------------------------------------------------------------------------
# agent_rules.get_rules_for_role safety
# ---------------------------------------------------------------------------


class TestAgentRulesSafeForAbsentRole:
    def test_unknown_role_returns_empty_list_not_keyerror(self):
        from hivepilot.agent_rules import get_rules_for_role

        rules = get_rules_for_role("this_role_does_not_exist")
        assert rules == []

    def test_known_role_still_returns_rules(self):
        from hivepilot.agent_rules import get_rules_for_role

        rules = get_rules_for_role("developer")
        assert isinstance(rules, list)
        assert len(rules) > 0


# ---------------------------------------------------------------------------
# cli.py --role default no longer assumes "ceo"
# ---------------------------------------------------------------------------


class TestCliDebateRoleDefault:
    def test_debate_role_default_is_developer(self):
        import inspect

        from hivepilot.cli import debate

        sig = inspect.signature(debate)
        default = sig.parameters["role"].default
        assert default.default == "developer"


# ---------------------------------------------------------------------------
# Existing roles.yaml-based configs keep working (full-replace loader
# unchanged) -- this repo's own root roles.yaml is untouched by Sprint 2.
# ---------------------------------------------------------------------------


class TestExistingRolesYamlConfigsUnaffected:
    def test_root_roles_yaml_still_defines_full_company_roster(self):
        from hivepilot.roles import ROLES

        assert REMOVED_BUSINESS_ROLES.issubset(set(ROLES))
        assert "developer" in ROLES

    def test_debate_path_unaffected_ceo_has_dual_models(self):
        from hivepilot.roles import get_role

        ceo = get_role("ceo")
        assert ceo.models and len(ceo.models) > 1, "CEO must keep its dual-model debate config"
