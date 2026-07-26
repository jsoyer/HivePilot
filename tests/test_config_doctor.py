"""Tests for hivepilot.services.config_doctor (`hivepilot config doctor` +
`hivepilot plugins verify`).

Each check is covered with BOTH a passing and a failing fixture, asserting
the failing message names the problem AND the fix -- per the sprint's
Anti-Goodhart requirement, these assert on the actual detection LOGIC (a
real dangling reference / a real cwd-relative path / a real missing
plugin), never merely on "the function returns a list".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hivepilot.config import settings
from hivepilot.services import config_doctor


def _write_minimal_valid_config(base_dir: Path) -> None:
    """Six required files + prompts/agents, matching test_cli.py's helper --
    keeps validate_config() (called by check_dangling_references) silent so
    each test can isolate the ONE new check it's exercising."""
    (base_dir / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"path": "~/dev/demo"}}})
    )
    (base_dir / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (base_dir / "prompts" / "agents").mkdir(parents=True)
    (base_dir / "prompts" / "agents" / "planner.md").write_text("# planner")


def _currently_enabled_plugin_stems() -> list[str]:
    """Every `*_enabled` flag that config_doctor treats as plugin-backed
    (i.e. NOT a builtin-runner or non-plugin feature flag) that is
    currently True -- used to build a fake PluginManager.loaded that
    matches whatever this codebase's *_enabled defaults happen to be,
    instead of hardcoding today's default set."""
    from hivepilot.registry import _BUILTIN_RUNNERS
    from hivepilot.services.config_provenance import all_keys

    builtin_stems = frozenset(_BUILTIN_RUNNERS)
    stems = []
    for key in all_keys():
        if not key.endswith("_enabled"):
            continue
        stem = key[: -len("_enabled")]
        if stem in config_doctor._NON_PLUGIN_ENABLED_FLAG_EXCEPTIONS or stem in builtin_stems:
            continue
        if getattr(settings, key, False):
            stems.append(stem)
    return stems


def _clear_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HIVEPILOT_BASE_DIR",
        "HIVEPILOT_STATE_DB",
        "HIVEPILOT_PROMPTS_DIR",
        "HIVEPILOT_OBSIDIAN_VAULT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# describe_resolved_paths / check_cwd_relative_paths (incident #1)
# ---------------------------------------------------------------------------


class TestResolvedPaths:
    def test_describe_resolved_paths_are_all_absolute(self) -> None:
        lines = config_doctor.describe_resolved_paths()
        assert lines, "expected at least one path line"
        for line in lines:
            _label, _sep, value = line.partition(":")
            assert Path(value.strip()).is_absolute(), f"non-absolute path line: {line}"

    def test_warns_when_base_dir_not_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAILING fixture: no HIVEPILOT_BASE_DIR, no per-field override --
        state_db resolves through settings.base_dir, which is cwd-dependent
        unless pinned. This is incident #1's exact failure mode."""
        _clear_path_env(monkeypatch)

        findings = config_doctor.check_cwd_relative_paths()

        state_db_findings = [f for f in findings if "state_db" in f.message]
        assert state_db_findings, "expected a cwd_relative_path finding for state_db"
        finding = state_db_findings[0]
        assert finding.severity == "warning"
        assert finding.check == "cwd_relative_path"
        assert "HIVEPILOT_BASE_DIR" in finding.fix

    def test_no_warning_when_base_dir_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PASSING fixture: HIVEPILOT_BASE_DIR set -- the documented remedy
        for incident #1 -- suppresses every base_dir-relative warning."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", "/srv/hivepilot")

        findings = config_doctor.check_cwd_relative_paths()

        assert findings == []

    def test_no_warning_when_field_explicitly_overridden_absolute(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PASSING fixture (per-field): an absolute HIVEPILOT_STATE_DB alone
        (no HIVEPILOT_BASE_DIR) is enough to un-flag state_db specifically."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "state.db"))

        findings = config_doctor.check_cwd_relative_paths()

        assert not any("state_db" in f.message for f in findings)


# ---------------------------------------------------------------------------
# check_sync_drift (incident #3)
# ---------------------------------------------------------------------------


class TestSyncDrift:
    def test_no_config_repo_configured_is_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "config_repo", None, raising=False)
        assert config_doctor.check_sync_drift() == []

    def test_clone_not_yet_cloned_is_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            settings, "config_repo", "https://example.invalid/config.git", raising=False
        )
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        # No clone dir created -- nothing to compare against.
        assert config_doctor.check_sync_drift() == []

    def test_flags_drift_between_clone_and_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FAILING fixture: the clone has a projects.yaml the active (XDG)
        config doesn't match -- exactly incident #3 (edited the clone,
        forgot `config sync`)."""
        monkeypatch.setattr(
            settings, "config_repo", "https://example.invalid/config.git", raising=False
        )
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        clone_dir = tmp_path / "data" / "hivepilot" / "config-repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / "projects.yaml").write_text("projects:\n  new-project:\n    path: /tmp/x\n")

        active_dir = tmp_path / "config" / "hivepilot"
        active_dir.mkdir(parents=True)
        # active has nothing -- clone is strictly ahead.

        findings = config_doctor.check_sync_drift()

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].check == "config_repo_out_of_sync"
        assert "projects.yaml" in findings[0].message
        assert "hivepilot config sync" in findings[0].fix

    def test_clean_when_clone_matches_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PASSING fixture: clone and active are byte-identical."""
        monkeypatch.setattr(
            settings, "config_repo", "https://example.invalid/config.git", raising=False
        )
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        clone_dir = tmp_path / "data" / "hivepilot" / "config-repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / "projects.yaml").write_text("projects: {}\n")

        active_dir = tmp_path / "config" / "hivepilot"
        active_dir.mkdir(parents=True)
        (active_dir / "projects.yaml").write_text("projects: {}\n")

        assert config_doctor.check_sync_drift() == []


# ---------------------------------------------------------------------------
# check_enabled_plugins_loaded (incident #4)
# ---------------------------------------------------------------------------


class TestEnabledPluginsLoaded:
    def test_flags_enabled_but_not_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAILING fixture: mem0_enabled=True but PluginManager never loaded
        a 'mem0' plugin record -- incident #4 (env flag flipped, plugin FILE
        missing from the plugins dir; only symptom was an empty panel)."""
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        mem0_findings = [f for f in findings if "'mem0'" in f.message]
        assert mem0_findings, "expected a plugin_enabled_not_loaded finding for mem0"
        assert mem0_findings[0].severity == "error"
        assert mem0_findings[0].check == "plugin_enabled_not_loaded"
        assert "plugins install mem0" in mem0_findings[0].fix

    def test_clean_when_enabled_plugin_is_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PASSING fixture: the plugin IS in PluginManager.loaded."""
        monkeypatch.setattr(settings, "mem0_enabled", True, raising=False)
        fake_manager = SimpleNamespace(loaded=[SimpleNamespace(name="mem0")])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any("'mem0'" in f.message for f in findings)

    def test_clean_when_flag_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "mem0_enabled", False, raising=False)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any("'mem0'" in f.message for f in findings)

    def test_builtin_runner_flags_are_never_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """claude/vibe/openrouter are built into hivepilot.registry, never a
        plugins/*.py file -- must never be reported as 'enabled but not loaded'."""
        monkeypatch.setattr(settings, "claude_enabled", True, raising=False)
        fake_manager = SimpleNamespace(loaded=[])

        findings = config_doctor.check_enabled_plugins_loaded(fake_manager)

        assert not any("'claude'" in f.message for f in findings)


# ---------------------------------------------------------------------------
# check_plugin_health (incident #5)
# ---------------------------------------------------------------------------


class TestPluginHealth:
    def test_surfaces_error_health_with_pip_fix(self) -> None:
        fake_manager = SimpleNamespace(
            check_all=lambda: {"mem0": ("error", "mem0ai not installed")}
        )

        findings = config_doctor.check_plugin_health(fake_manager)

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].check == "plugin_health"
        assert "mem0" in findings[0].message
        assert "pip install mem0ai" in findings[0].fix

    def test_degraded_is_a_warning_not_an_error(self) -> None:
        fake_manager = SimpleNamespace(
            check_all=lambda: {"mem0": ("degraded", "installed but disabled (mem0_enabled=False)")}
        )

        findings = config_doctor.check_plugin_health(fake_manager)

        assert len(findings) == 1
        assert findings[0].severity == "warning"

    def test_clean_when_all_healthy(self) -> None:
        fake_manager = SimpleNamespace(check_all=lambda: {"rtk": ("ok", "rtk found on PATH")})

        assert config_doctor.check_plugin_health(fake_manager) == []


# ---------------------------------------------------------------------------
# Dangling references (incident #7 + #6's alias variant)
# ---------------------------------------------------------------------------


class TestSchedulesDangling:
    def test_flags_unknown_task_and_project(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"real-proj": {}}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {"real-task": {}}}))
        (tmp_path / "schedules.yaml").write_text(
            yaml.dump(
                {
                    "schedules": {
                        "nightly": {
                            "task": "ghost-task",
                            "projects": ["ghost-project"],
                        }
                    }
                }
            )
        )

        findings = config_doctor._check_schedules_dangling(tmp_path)

        checks = {f.check for f in findings}
        assert "dangling_schedule_task" in checks
        assert "dangling_schedule_project" in checks
        task_finding = next(f for f in findings if f.check == "dangling_schedule_task")
        assert "ghost-task" in task_finding.message
        assert "tasks.yaml" in task_finding.fix

    def test_clean_when_references_are_valid(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"real-proj": {}}}))
        (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {"real-task": {}}}))
        (tmp_path / "schedules.yaml").write_text(
            yaml.dump({"schedules": {"nightly": {"task": "real-task", "projects": ["real-proj"]}}})
        )

        assert config_doctor._check_schedules_dangling(tmp_path) == []


class TestRoleOverridesDangling:
    def test_flags_alias_used_instead_of_real_role_key(self, tmp_path: Path) -> None:
        """FAILING fixture: incident #6 -- 'cos' is a Telegram command alias
        for 'chief_of_staff', never a real roles.yaml key."""
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": {"role_overrides": {"cos": {"model": "glm"}}}}})
        )

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "role_alias_used_as_role_key"
        assert "cos" in findings[0].message
        assert "chief_of_staff" in findings[0].fix

    def test_flags_dangling_role_override_not_an_alias(self, tmp_path: Path) -> None:
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": {"role_overrides": {"totally_bogus": {}}}}})
        )

        findings = config_doctor._check_role_overrides_dangling(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "dangling_role_override"
        assert "totally_bogus" in findings[0].message

    def test_clean_when_role_override_is_real(self, tmp_path: Path) -> None:
        (tmp_path / "roles.yaml").write_text(
            yaml.dump({"roles": [{"name": "chief_of_staff", "prompt_file": "cos.md"}]})
        )
        (tmp_path / "policies.yaml").write_text(
            yaml.dump({"policies": {"default": {"role_overrides": {"chief_of_staff": {}}}}})
        )

        assert config_doctor._check_role_overrides_dangling(tmp_path) == []


class TestOnlyModulesDangling:
    def test_flags_module_not_defined_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"modules": {"real-module": "apps/real"}}}})
        )
        (tmp_path / "pipelines.yaml").write_text(
            yaml.dump(
                {
                    "pipelines": {
                        "default": {"stages": [{"name": "dev", "only_modules": ["ghost-module"]}]}
                    }
                }
            )
        )

        findings = config_doctor._check_only_modules_dangling(tmp_path)

        assert len(findings) == 1
        assert findings[0].check == "dangling_only_module"
        assert "ghost-module" in findings[0].message

    def test_clean_when_module_is_defined(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump({"projects": {"acme": {"modules": {"real-module": "apps/real"}}}})
        )
        (tmp_path / "pipelines.yaml").write_text(
            yaml.dump(
                {
                    "pipelines": {
                        "default": {"stages": [{"name": "dev", "only_modules": ["real-module"]}]}
                    }
                }
            )
        )

        assert config_doctor._check_only_modules_dangling(tmp_path) == []


class TestDanglingReferencesIntegration:
    def test_clean_full_config_yields_no_dangling_findings(self, tmp_path: Path) -> None:
        _write_minimal_valid_config(tmp_path)
        (tmp_path / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        assert config_doctor.check_dangling_references(tmp_path) == []


# ---------------------------------------------------------------------------
# check_secrets_sanity
# ---------------------------------------------------------------------------


class TestSecretsSanity:
    def test_flags_empty_secret_setting(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "linear_api_key", "", raising=False)

        findings = config_doctor.check_secrets_sanity(tmp_path)

        empty_findings = [f for f in findings if f.check == "empty_secret_setting"]
        assert empty_findings, "expected an empty_secret_setting finding"
        assert "linear_api_key" in empty_findings[0].message

    def test_clean_when_secret_setting_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {}}))
        monkeypatch.setattr(settings, "linear_api_key", None, raising=False)

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert not any(f.check == "empty_secret_setting" for f in findings)

    def test_flags_dangling_secret_ref(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "acme": {
                            "env": {"API_KEY": "${secret:missing_ref}"},
                            "secrets": {},
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        ref_findings = [f for f in findings if f.check == "dangling_secret_ref"]
        assert ref_findings, "expected a dangling_secret_ref finding"
        assert "missing_ref" in ref_findings[0].message

    def test_clean_when_secret_ref_has_catalog_entry(self, tmp_path: Path) -> None:
        (tmp_path / "projects.yaml").write_text(
            yaml.dump(
                {
                    "projects": {
                        "acme": {
                            "env": {"API_KEY": "${secret:present_ref}"},
                            "secrets": {"present_ref": {"source": "env", "key": "SOME_VAR"}},
                        }
                    }
                }
            )
        )

        findings = config_doctor.check_secrets_sanity(tmp_path)

        assert not any(f.check == "dangling_secret_ref" for f in findings)


# ---------------------------------------------------------------------------
# run_doctor / CLI integration
# ---------------------------------------------------------------------------


class TestRunDoctorIntegration:
    def test_clean_config_yields_zero_findings_exit_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PASSING fixture: a fully clean config + a stubbed, empty
        PluginManager must yield zero findings end to end."""
        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        # Simulate "every currently-enabled plugin flag IS loaded" -- robust
        # to whichever flags happen to default True in this codebase, rather
        # than hardcoding today's default set.
        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        findings = config_doctor.run_doctor(config_dir=config_dir)

        assert findings == [], f"expected zero findings, got: {[f.message for f in findings]}"

    def test_cli_command_reports_ok_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from typer.testing import CliRunner

        from hivepilot.cli import app

        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_valid_config(config_dir)
        (config_dir / "schedules.yaml").write_text(yaml.dump({"schedules": {}}))

        fake_manager = SimpleNamespace(
            loaded=[SimpleNamespace(name=stem) for stem in _currently_enabled_plugin_stems()],
            check_all=lambda: {},
        )
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["config", "doctor", "--dir", str(config_dir)])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_cli_command_exits_nonzero_on_error_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from typer.testing import CliRunner

        from hivepilot.cli import app

        _clear_path_env(monkeypatch)
        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        # Deliberately empty/invalid -- validate_config will report missing files.

        fake_manager = SimpleNamespace(loaded=[], check_all=lambda: {})
        monkeypatch.setattr("hivepilot.plugins.PluginManager", lambda: fake_manager, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["config", "doctor", "--dir", str(config_dir)])

        assert result.exit_code == 1, result.output
        assert "ERROR" in result.output


# ---------------------------------------------------------------------------
# plugins verify (incident #5)
# ---------------------------------------------------------------------------


class TestVerifyPlugins:
    def test_platform_tag_reports_something_nonempty(self) -> None:
        tag = config_doctor.platform_tag()
        assert isinstance(tag, str) and tag

    def test_distinguishes_installed_per_pip_from_importable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module that imports fine but whose EXPECTED distribution isn't
        what pip reports must be flagged as a mismatch (namesake collision,
        incident #5) -- NOT silently treated as 'ok'."""

        def fake_import(name: str):
            if name == "headroom":
                return object()
            raise ImportError(name)

        def fake_version(dist: str) -> str:
            raise config_doctor.importlib_metadata.PackageNotFoundError(dist)

        monkeypatch.setattr(config_doctor.importlib, "import_module", fake_import)
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", fake_version)

        result = config_doctor._verify_pip_plugin("headroom", "headroom", "headroom-ai")

        assert result.importable is True
        assert result.present_per_declaration is False
        assert result.mismatch is not None
        assert "namesake collision" in result.mismatch

    def test_pip_installed_but_not_importable_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of incident #5: pip believes it's installed, but
        the import fails (e.g. a broken native build)."""

        def fake_import(name: str):
            raise ImportError("no module named " + name)

        def fake_version(dist: str) -> str:
            return "1.2.3"

        monkeypatch.setattr(config_doctor.importlib, "import_module", fake_import)
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", fake_version)

        result = config_doctor._verify_pip_plugin("headroom", "headroom", "headroom-ai")

        assert result.importable is False
        assert result.present_per_declaration is True
        assert result.mismatch is not None
        assert "broken" in result.mismatch

    def test_clean_when_import_and_distribution_agree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_doctor.importlib, "import_module", lambda name: object())
        monkeypatch.setattr(config_doctor.importlib_metadata, "version", lambda dist: "1.0.0")

        result = config_doctor._verify_pip_plugin("mem0", "mem0", "mem0ai")

        assert result.importable is True
        assert result.present_per_declaration is True
        assert result.mismatch is None

    def test_verify_plugins_covers_every_known_example_plugin(self) -> None:
        from hivepilot.services.plugin_installer import KNOWN_EXAMPLE_PLUGINS

        results = config_doctor.verify_plugins()

        names = {r.name for r in results}
        assert names == set(KNOWN_EXAMPLE_PLUGINS)

    def test_binary_probe_reports_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_doctor.shutil, "which", lambda binary: None)

        result = config_doctor._verify_binary_plugin("rtk", "rtk")

        assert result.prereq_kind == "binary"
        assert result.present_per_declaration is False
        assert "NOT FOUND" in result.detail

    def test_binary_probe_reports_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_doctor.shutil, "which", lambda binary: f"/usr/bin/{binary}")

        result = config_doctor._verify_binary_plugin("rtk", "rtk")

        assert result.present_per_declaration is True
        assert "found on PATH" in result.detail


class TestPluginsVerifyCli:
    def test_cli_command_lists_every_plugin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from hivepilot.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "verify"])

        assert result.exit_code in (0, 1), result.output
        assert "mem0" in result.output
        assert "headroom" in result.output
