"""
Tests for the `hugo` runner plugin (Phase 25).

`plugins/hugo.py` is a local-file plugin (see docs/v4/PLUGINS.md) that wraps
the Hugo static-site-generator CLI: operations `build` (default) / `new` /
`serve`, resolved from `payload.step.command` / `self.definition.command` /
`self.definition.options["operation"]` — exactly the operation-resolution
pattern used by the IaC/Helm runners
(`hivepilot.runners.iac_runner`/`helm_runner`).

Covers:
(a) `register()` exposes runner kind `hugo` + a `health` check, gated by
    `settings.hugo_enabled`.
(b) `_build_command()` argv per operation (build/new/serve, with options).
(c) `run()` invokes `hugo <argv>` with a merged env (project/definition/
    secrets), raises `RuntimeError` when `hugo` isn't on PATH, and
    `ValueError` on `new` missing `options.path` / an unknown operation.
(d) `health()` reflects `shutil.which("hugo")`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
HUGO_PLUGIN_PATH = REPO_ROOT / "plugins" / "hugo.py"


def _load_hugo_module() -> ModuleType:
    """Load plugins/hugo.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_hugo_test", HUGO_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hugo_module() -> ModuleType:
    return _load_hugo_module()


def _payload(
    tmp_path: Path,
    command: str | None = None,
    options: dict | None = None,
    secrets: dict | None = None,
) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="hugo", command=command),
        metadata={},
        secrets=secrets or {},
    )


def _runner(hugo_module: ModuleType, options: dict | None = None) -> Any:
    return hugo_module.HugoRunner(
        RunnerDefinition(name="hugo", kind="hugo", options=options or {}), settings
    )


class TestRegister:
    def test_register_exposes_hugo_kind(self, hugo_module: ModuleType) -> None:
        hooks = hugo_module.register()
        assert "runners" in hooks
        assert "hugo" in hooks["runners"]

    def test_hugo_kind_does_not_collide_with_a_built_in(self) -> None:
        assert "hugo" not in KNOWN_RUNNER_KINDS

    def test_register_exposes_health_check(self, hugo_module: ModuleType) -> None:
        hooks = hugo_module.register()
        assert "health" in hooks
        assert "hugo" in hooks["health"]
        assert hooks["health"]["hugo"] is hugo_module.health

    def test_register_returns_contributions_when_enabled_by_default(
        self, hugo_module: ModuleType
    ) -> None:
        assert settings.hugo_enabled is True
        hooks = hugo_module.register()
        assert hooks["runners"] == {"hugo": hugo_module.HugoRunner}
        assert "hugo" in hooks["health"]

    def test_register_returns_empty_when_disabled(
        self, hugo_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "hugo_enabled", False, raising=False)
        assert hugo_module.register() == {}


class TestHealth:
    def test_ok_when_hugo_on_path(self, hugo_module: ModuleType) -> None:
        with patch.object(hugo_module.shutil, "which", return_value="/usr/local/bin/hugo"):
            result = hugo_module.health()
        assert result.status == "ok"

    def test_error_when_hugo_not_on_path(self, hugo_module: ModuleType) -> None:
        with patch.object(hugo_module.shutil, "which", return_value=None):
            result = hugo_module.health()
        assert result.status == "error"
        assert "not on PATH" in result.detail

    def test_health_is_keyword_tolerant(self, hugo_module: ModuleType) -> None:
        with patch.object(hugo_module.shutil, "which", return_value=None):
            result = hugo_module.health(project="anything")
        assert result.status == "error"


class TestBuildCommand:
    def test_build_default_minifies(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        assert runner._build_command("build", {}) == ["hugo", "--minify"]

    def test_build_with_destination_base_url_environment(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        argv = runner._build_command(
            "build",
            {
                "destination": "public",
                "base_url": "https://x",
                "environment": "production",
            },
        )
        assert argv[0] == "hugo"
        assert "--minify" in argv
        assert argv[argv.index("--destination") + 1] == "public"
        assert argv[argv.index("--baseURL") + 1] == "https://x"
        assert argv[argv.index("--environment") + 1] == "production"

    def test_build_minify_false_omits_flag(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        argv = runner._build_command("build", {"minify": False})
        assert argv == ["hugo"]
        assert "--minify" not in argv

    def test_new_requires_path(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        assert runner._build_command("new", {"path": "posts/x.md"}) == [
            "hugo",
            "new",
            "posts/x.md",
        ]

    def test_new_with_archetype(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        argv = runner._build_command("new", {"path": "posts/x.md", "archetype": "post"})
        assert argv[:3] == ["hugo", "new", "posts/x.md"]
        assert argv[argv.index("--kind") + 1] == "post"

    def test_new_missing_path_raises_value_error(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        with pytest.raises(ValueError):
            runner._build_command("new", {})

    def test_new_empty_path_raises_value_error(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        with pytest.raises(ValueError):
            runner._build_command("new", {"path": ""})

    def test_serve_default(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        assert runner._build_command("serve", {}) == ["hugo", "serve"]

    def test_serve_with_bind_and_port(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        argv = runner._build_command("serve", {"bind": "0.0.0.0", "port": 1414})
        assert argv[0:2] == ["hugo", "serve"]
        assert argv[argv.index("--bind") + 1] == "0.0.0.0"
        assert argv[argv.index("--port") + 1] == "1414"

    def test_unknown_operation_raises_value_error(self, hugo_module: ModuleType) -> None:
        runner = _runner(hugo_module)
        with pytest.raises(ValueError):
            runner._build_command("nonsense", {})


class TestResolveOperation:
    def test_defaults_to_build(self, hugo_module: ModuleType, tmp_path: Path) -> None:
        runner = _runner(hugo_module)
        assert runner._resolve_operation(_payload(tmp_path)) == "build"

    def test_step_command_wins(self, hugo_module: ModuleType, tmp_path: Path) -> None:
        runner = _runner(hugo_module)
        assert runner._resolve_operation(_payload(tmp_path, command="Serve")) == "serve"

    def test_definition_options_operation_used_when_no_command(
        self, hugo_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = _runner(hugo_module, options={"operation": "new"})
        assert runner._resolve_operation(_payload(tmp_path)) == "new"


class TestRun:
    def test_run_invokes_hugo_build_by_default(
        self, hugo_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = _runner(hugo_module)
        with (
            patch.object(hugo_module.shutil, "which", return_value="/usr/local/bin/hugo"),
            patch.object(hugo_module.subprocess, "run") as mock_run,
        ):
            runner.run(_payload(tmp_path))

        args = mock_run.call_args.args[0]
        assert args == ["hugo", "--minify"]
        assert mock_run.call_args.kwargs["check"] is True
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_run_missing_hugo_raises_runtime_error(
        self, hugo_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = _runner(hugo_module)
        with (
            patch.object(hugo_module.shutil, "which", return_value=None),
            patch.object(hugo_module.subprocess, "run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path))
        mock_run.assert_not_called()

    def test_run_new_missing_path_raises_value_error(
        self, hugo_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = _runner(hugo_module)
        with (
            patch.object(hugo_module.shutil, "which", return_value="/usr/local/bin/hugo"),
            patch.object(hugo_module.subprocess, "run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, command="new"))

    def test_run_unknown_operation_raises_value_error(
        self, hugo_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = _runner(hugo_module)
        with (
            patch.object(hugo_module.shutil, "which", return_value="/usr/local/bin/hugo"),
            patch.object(hugo_module.subprocess, "run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, command="nonsense"))

    def test_run_merges_secrets_into_env(self, hugo_module: ModuleType, tmp_path: Path) -> None:
        runner = _runner(hugo_module)
        with (
            patch.object(hugo_module.shutil, "which", return_value="/usr/local/bin/hugo"),
            patch.object(hugo_module.subprocess, "run") as mock_run,
        ):
            runner.run(_payload(tmp_path, secrets={"HUGO_TOKEN": "shh"}))

        env = mock_run.call_args.kwargs["env"]
        assert env["HUGO_TOKEN"] == "shh"


class TestPluginManagerDiscoversHugo:
    @pytest.fixture(autouse=True)
    def _restore_runner_map(self):
        """RUNNER_MAP is process-global mutable state — snapshot/restore
        around every test here so `hugo` (registered by the real
        `plugins/hugo.py` on disk) never leaks into other test modules
        sharing the pytest session (same pattern as test_rtk.py)."""
        from hivepilot.registry import RUNNER_MAP

        snapshot = dict(RUNNER_MAP)
        yield
        RUNNER_MAP.clear()
        RUNNER_MAP.update(snapshot)

    def test_plugin_manager_registers_hugo_into_runner_map(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "hugo" in RUNNER_MAP
        assert any(r.source == "local-file" and r.name == "hugo" for r in pm.loaded)

    def test_plugin_manager_skips_hugo_when_disabled(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "hugo_enabled", False, raising=False)
        RUNNER_MAP.pop("hugo", None)  # clean baseline (fixture restores after)

        plugins_mod.PluginManager()

        assert "hugo" not in RUNNER_MAP
