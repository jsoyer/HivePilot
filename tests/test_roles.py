"""
Sprint 1.3 — Role abstraction tests.

Covers:
- All 8 roles are present in ROLES registry
- Each role's prompt_file exists on disk and is non-empty
- Each role's model_profile is a valid claude_profiles key in model_profiles.yaml
- list_roles() returns roles ordered by their pipeline position (order field)
- get_role() returns the expected Role instance with correct fields
- Role.effort / resolve_runner's 3-tuple (runner, model, effort) return
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


class TestPromptFileConfigChainResolution:
    """Role.prompt_file must resolve through Settings.resolve_config_path() so a
    prompt override placed in the config repo is picked up, with the packaged
    prompts/agents/ copy as the final fallback (Sprint 2)."""

    def test_config_repo_override_wins_over_package_copy(self, tmp_path, monkeypatch):
        # Isolate from any real ~/.config/hivepilot on the host machine.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        from hivepilot.config import Settings
        from hivepilot.roles import _PROMPTS_DIR, _resolve_prompt_path

        config_repo = tmp_path / "config_repo"
        agents_dir = config_repo / "prompts" / "agents"
        agents_dir.mkdir(parents=True)
        override_file = agents_dir / "ceo.md"
        override_file.write_text("# Overridden CEO prompt\n", encoding="utf-8")

        test_settings = Settings(config_repo=str(config_repo), base_dir=tmp_path)
        resolved = _resolve_prompt_path("ceo.md", test_settings)

        assert resolved == override_file, (
            f"Expected config-repo override {override_file}, got {resolved}"
        )
        assert resolved != _PROMPTS_DIR / "ceo.md"
        assert resolved.read_text(encoding="utf-8") == "# Overridden CEO prompt\n"

    def test_missing_override_falls_back_to_package_copy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        from hivepilot.config import Settings
        from hivepilot.roles import _PROMPTS_DIR, _resolve_prompt_path

        # config_repo exists locally but has no prompts/agents/ceo.md override.
        config_repo = tmp_path / "config_repo"
        config_repo.mkdir()

        test_settings = Settings(config_repo=str(config_repo), base_dir=tmp_path)
        resolved = _resolve_prompt_path("ceo.md", test_settings)

        assert resolved == _PROMPTS_DIR / "ceo.md", (
            "Missing config-repo override must fall back to the packaged copy"
        )
        assert resolved.exists()

    def test_missing_override_and_missing_package_copy_never_crashes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        from hivepilot.config import Settings
        from hivepilot.roles import _resolve_prompt_path

        test_settings = Settings(config_repo=None, base_dir=tmp_path)
        resolved = _resolve_prompt_path("does-not-exist-anywhere.md", test_settings)

        # Never raises; callers guard with `.exists()` -> "" at the call sites.
        assert isinstance(resolved, Path)
        assert not resolved.exists()

    def test_load_roles_uses_config_chain_for_prompt_file(self, tmp_path, monkeypatch):
        """load_roles() itself must route prompt_file resolution through the
        config chain (not just the standalone helper)."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        import hivepilot.config as config_module
        from hivepilot.roles import load_roles

        config_repo = tmp_path / "config_repo"
        agents_dir = config_repo / "prompts" / "agents"
        agents_dir.mkdir(parents=True)
        override_file = agents_dir / "ceo.md"
        override_file.write_text("# Overridden CEO prompt\n", encoding="utf-8")

        # roles_file resolves to the real repo roles.yaml (base_dir = repo root).
        test_settings = config_module.Settings(config_repo=str(config_repo), base_dir=REPO_ROOT)

        original_settings = config_module.settings
        try:
            config_module.settings = test_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        assert loaded["ceo"].prompt_file == override_file


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


# ---------------------------------------------------------------------------
# Reasoning-effort knob — Role.effort + resolve_runner's 3-tuple return
# ---------------------------------------------------------------------------


class TestRoleEffort:
    """Role.effort is an optional string validated against EFFORT_LEVELS
    (hivepilot.models), shared with TaskStep.effort — see hivepilot/models.py."""

    def _base_kwargs(self) -> dict:
        return {
            "name": "x",
            "title": "X",
            "prompt_file": Path("x.md"),
            "model_profile": "coding",
            "inputs": [],
            "outputs": [],
            "can_block": False,
            "order": 99,
        }

    def test_role_effort_accepts_valid_level(self):
        from hivepilot.roles import Role

        role = Role(**self._base_kwargs(), effort="high")
        assert role.effort == "high"

    def test_role_effort_rejects_invalid_level(self):
        import pydantic

        from hivepilot.roles import Role

        with pytest.raises(pydantic.ValidationError):
            Role(**self._base_kwargs(), effort="bogus")

    def test_role_effort_defaults_to_none(self):
        from hivepilot.roles import Role

        role = Role(**self._base_kwargs())
        assert role.effort is None


class TestResolveRunnerEffort:
    """resolve_runner() now returns a 3-tuple (runner_kind, model, effort)."""

    def test_resolve_runner_returns_three_tuple(self):
        from hivepilot.roles import resolve_runner

        result = resolve_runner("developer")
        assert len(result) == 3

    def test_resolve_runner_no_effort_yields_none(self):
        """The built-in `developer` role declares no effort -- resolve_runner's
        3rd element must be None, never invented (regression guard: every
        existing role binding stays byte-identical)."""
        from hivepilot.roles import resolve_runner

        runner_kind, model, effort = resolve_runner("developer")
        assert runner_kind == "claude"
        assert effort is None

    def test_resolve_runner_returns_roles_effort(self, monkeypatch):
        """A role with an explicit `effort` yields it as resolve_runner's 3rd
        tuple element."""
        from hivepilot.roles import ROLES, Role, resolve_runner

        original = ROLES["developer"]
        effortful = original.model_copy(update={"effort": "high"})
        monkeypatch.setitem(ROLES, "developer", effortful)

        runner_kind, model, effort = resolve_runner("developer")
        assert effort == "high"
        assert isinstance(effortful, Role)


class TestRefreshRolesHotReload:
    """Phase 14c — refresh_roles() hot-reload: fail-closed TO THE PREVIOUS
    live config (never downgraded to _DEFAULT_ROLES), and the strict/
    non-strict loader split (_load_roles_strict raises, load_roles never
    does)."""

    def _mock_settings(self, roles_path: Path):
        return type(
            "MockSettings",
            (),
            {
                "roles_file": roles_path,
                "resolve_config_path": lambda self, f: roles_path,
            },
        )()

    def _valid_role_yaml(self, prompt_path: Path) -> str:
        return f"""
roles:
  - name: tester
    title: Tester
    prompt_file: {prompt_path.name}
    model_profile: coding
    inputs: []
    outputs: []
    can_block: false
    order: 1
"""

    def test_refresh_roles_returns_true_and_swaps_on_valid_new_config(self, tmp_path, monkeypatch):
        import hivepilot.config as config_module
        from hivepilot import roles as roles_module

        prompt_file = tmp_path / "tester.md"
        prompt_file.write_text("You are a tester.")
        roles_path = tmp_path / "roles.yaml"
        roles_path.write_text(self._valid_role_yaml(prompt_file))

        original_settings = config_module.settings
        original_roles = dict(roles_module.ROLES)
        try:
            config_module.settings = self._mock_settings(roles_path)
            ok = roles_module.refresh_roles()
            assert ok is True
            assert set(roles_module.ROLES.keys()) == {"tester"}
            assert roles_module.ROLES["tester"].title == "Tester"
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original_roles

    def test_refresh_roles_returns_false_and_keeps_previous_rich_roles_on_broken_file(
        self, tmp_path, monkeypatch
    ):
        """The key fail-closed guarantee: a broken roles.yaml deployed to a
        running process must NOT silently downgrade a rich, already-loaded
        roster down to the generic single-`developer` `_DEFAULT_ROLES`
        fallback -- the previous config is kept verbatim."""
        import hivepilot.config as config_module
        from hivepilot import roles as roles_module
        from hivepilot.roles import _DEFAULT_ROLES

        broken_path = tmp_path / "broken_roles.yaml"
        broken_path.write_text("roles: [{name: bad, missing_required_fields: true}]")

        # Simulate a "rich" already-loaded roster distinct from _DEFAULT_ROLES
        # (the real repo roles.yaml already provides one, but build an
        # explicit synthetic snapshot so this test doesn't depend on the
        # repo's roles.yaml staying exactly as-is).
        rich_snapshot = dict(roles_module.ROLES)
        rich_snapshot["extra_role"] = _DEFAULT_ROLES["developer"].model_copy(
            update={"name": "extra_role"}
        )

        original_settings = config_module.settings
        original_roles = dict(roles_module.ROLES)
        try:
            roles_module.ROLES = rich_snapshot
            config_module.settings = self._mock_settings(broken_path)
            ok = roles_module.refresh_roles()
            assert ok is False
            # Kept the previous (rich) config verbatim -- NOT downgraded to
            # _DEFAULT_ROLES, and NOT silently mutated either.
            assert roles_module.ROLES == rich_snapshot
            assert "extra_role" in roles_module.ROLES
            assert set(roles_module.ROLES.keys()) != set(_DEFAULT_ROLES.keys())
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original_roles

    def test_refresh_roles_returns_false_on_missing_file(self, tmp_path, monkeypatch):
        import hivepilot.config as config_module
        from hivepilot import roles as roles_module

        missing_path = tmp_path / "does_not_exist.yaml"

        original_settings = config_module.settings
        original_roles = dict(roles_module.ROLES)
        try:
            config_module.settings = self._mock_settings(missing_path)
            ok = roles_module.refresh_roles()
            assert ok is False
            assert roles_module.ROLES == original_roles
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original_roles

    def test_load_roles_strict_raises_on_bad_file(self, tmp_path):
        import hivepilot.config as config_module
        from hivepilot.roles import _load_roles_strict

        missing_path = tmp_path / "does_not_exist.yaml"
        original_settings = config_module.settings
        try:
            config_module.settings = self._mock_settings(missing_path)
            with pytest.raises(FileNotFoundError):
                _load_roles_strict()
        finally:
            config_module.settings = original_settings

    def test_load_roles_bootstrap_still_returns_defaults_on_same_bad_file(self, tmp_path):
        """load_roles() (the non-raising bootstrap loader) must be
        byte-identical in behavior to before this change -- it still
        swallows the exception and returns _DEFAULT_ROLES."""
        import hivepilot.config as config_module
        from hivepilot.roles import _DEFAULT_ROLES, load_roles

        missing_path = tmp_path / "does_not_exist.yaml"
        original_settings = config_module.settings
        try:
            config_module.settings = self._mock_settings(missing_path)
            result = load_roles()
            assert result == _DEFAULT_ROLES
        finally:
            config_module.settings = original_settings
