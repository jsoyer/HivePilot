"""
Tests for the `gh` runner plugin (S4 of the plugin-arch-overhaul follow-on).

`plugins/gh.py` is a self-contained, PATH-gated, opt-in (`gh_enabled`) local-file
plugin that executes the GitHub CLI (`gh <args>`) as a plain COMMAND-BASED runner
— NOT an LLM/prompt agent (unlike antigravity/kimi/qwen). It runs the operator-
specified `gh` subcommand from the step config.

Covers:
(a) `register()`: `{}` when `gh_enabled=False`; `{}` when `gh` not on PATH; full
    contribution dict when both enabled and the binary is present.
(b) `run()`: builds `["gh", <args...>]` from `step.command` (list-argv,
    `shell=True` never used); missing binary raises `RuntimeError`; empty/missing
    command raises `ValueError`; secrets land in `env` and never in `argv`.
(c) `is_destructive()`: True for known-destructive gh operations (`pr merge`,
    `repo delete`, `release delete`, `secret set`/`delete`, …), False for
    read-only/non-destructive ones, and False (documented fail-safe default) for
    an empty/unparseable command.
(d) `health()`: ok/error based on `shutil.which("gh")`.
(e) Registry: `resolve_runner_class("gh")` works via the real `PluginManager`
    local-file discovery mechanism (mirrors `tests/test_rtk.py`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
GH_PLUGIN_PATH = REPO_ROOT / "plugins" / "gh.py"


def _load_gh_module() -> ModuleType:
    """Load plugins/gh.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_gh_test", GH_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def gh_module() -> ModuleType:
    return _load_gh_module()


def _payload(
    tmp_path: Path, command: str | None = "pr create --title X", secrets: dict | None = None
) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="gh", command=command),
        metadata={},
        secrets=secrets or {},
    )


class TestRegister:
    def test_gh_kind_does_not_collide_with_a_built_in(self) -> None:
        assert "gh" not in KNOWN_RUNNER_KINDS

    def test_register_returns_empty_when_disabled(self, gh_module: ModuleType, monkeypatch) -> None:
        monkeypatch.setattr(settings, "gh_enabled", False, raising=False)
        with patch.object(gh_module.shutil, "which", return_value="/usr/bin/gh"):
            assert gh_module.register() == {}

    def test_register_returns_empty_when_binary_missing(
        self, gh_module: ModuleType, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "gh_enabled", True, raising=False)
        with patch.object(gh_module.shutil, "which", return_value=None):
            assert gh_module.register() == {}

    def test_register_returns_contributions_when_enabled_and_binary_present(
        self, gh_module: ModuleType, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "gh_enabled", True, raising=False)
        with patch.object(gh_module.shutil, "which", return_value="/usr/bin/gh"):
            hooks = gh_module.register()
        assert hooks["runners"] == {"gh": gh_module.GhRunner}
        assert hooks["health"] == {"gh": gh_module.health}


class TestHealth:
    def test_ok_when_gh_on_path(self, gh_module: ModuleType) -> None:
        with patch.object(gh_module.shutil, "which", return_value="/usr/bin/gh"):
            result = gh_module.health()
        assert result.status == "ok"

    def test_error_when_gh_not_on_path(self, gh_module: ModuleType) -> None:
        with patch.object(gh_module.shutil, "which", return_value=None):
            result = gh_module.health()
        assert result.status == "error"
        assert "not on PATH" in result.detail

    def test_health_is_keyword_tolerant(self, gh_module: ModuleType) -> None:
        with patch.object(gh_module.shutil, "which", return_value=None):
            result = gh_module.health(project="anything")
        assert result.status == "error"


class TestRun:
    def test_run_builds_gh_argv_from_step_command(
        self, gh_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = gh_module.GhRunner(RunnerDefinition(name="gh", kind="gh"), settings)
        with (
            patch.object(gh_module.shutil, "which", return_value="/usr/bin/gh"),
            patch.object(gh_module.subprocess, "run") as mock_run,
        ):
            runner.run(_payload(tmp_path, command="pr create --title X"))

        args = mock_run.call_args.args[0]
        assert args == ["gh", "pr", "create", "--title", "X"]
        assert mock_run.call_args.kwargs["check"] is True
        assert mock_run.call_args.kwargs.get("shell") is not True

    def test_run_renders_step_command_template(self, gh_module: ModuleType, tmp_path: Path) -> None:
        runner = gh_module.GhRunner(RunnerDefinition(name="gh", kind="gh"), settings)
        with (
            patch.object(gh_module.shutil, "which", return_value="/usr/bin/gh"),
            patch.object(gh_module.subprocess, "run") as mock_run,
        ):
            runner.run(_payload(tmp_path, command="pr view {task_name}"))

        args = mock_run.call_args.args[0]
        assert args == ["gh", "pr", "view", "t"]

    def test_missing_binary_raises_runtime_error(
        self, gh_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = gh_module.GhRunner(RunnerDefinition(name="gh", kind="gh"), settings)
        with patch.object(gh_module.shutil, "which", return_value=None):
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                runner.run(_payload(tmp_path))

    def test_empty_command_raises_value_error(self, gh_module: ModuleType, tmp_path: Path) -> None:
        runner = gh_module.GhRunner(RunnerDefinition(name="gh", kind="gh"), settings)
        with patch.object(gh_module.shutil, "which", return_value="/usr/bin/gh"):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, command=None))

    def test_secrets_land_in_env_not_argv(self, gh_module: ModuleType, tmp_path: Path) -> None:
        runner = gh_module.GhRunner(RunnerDefinition(name="gh", kind="gh"), settings)
        with (
            patch.object(gh_module.shutil, "which", return_value="/usr/bin/gh"),
            patch.object(gh_module.subprocess, "run") as mock_run,
        ):
            runner.run(
                _payload(
                    tmp_path,
                    command="pr create --title X",
                    secrets={"GH_TOKEN": "super-secret-value"},
                )
            )

        args = mock_run.call_args.args[0]
        assert "super-secret-value" not in args
        assert not any("super-secret-value" in a for a in args)
        env = mock_run.call_args.kwargs["env"]
        assert env["GH_TOKEN"] == "super-secret-value"


class TestIsDestructive:
    runner_factory = None

    @pytest.fixture(autouse=True)
    def _runner(self, gh_module: ModuleType):
        self.runner = gh_module.GhRunner(RunnerDefinition(name="gh", kind="gh"), settings)

    @pytest.mark.parametrize(
        "command",
        ["pr merge", "repo delete", "release delete", "secret set", "secret delete"],
    )
    def test_destructive_commands(self, command: str, tmp_path: Path) -> None:
        assert self.runner.is_destructive(_payload(tmp_path, command=command)) is True

    @pytest.mark.parametrize(
        "command",
        ["pr create", "issue list", "pr view", "repo clone octo/hello"],
    )
    def test_non_destructive_commands(self, command: str, tmp_path: Path) -> None:
        assert self.runner.is_destructive(_payload(tmp_path, command=command)) is False

    def test_empty_command_is_not_destructive(self, tmp_path: Path) -> None:
        assert self.runner.is_destructive(_payload(tmp_path, command=None)) is False

    def test_unparseable_command_is_not_destructive(self, tmp_path: Path) -> None:
        assert (
            self.runner.is_destructive(_payload(tmp_path, command='pr merge "unterminated'))
            is False
        )


class TestPluginManagerDiscoversGh:
    @pytest.fixture(autouse=True)
    def _restore_runner_map(self):
        """RUNNER_MAP is process-global mutable state — snapshot/restore around
        every test here so `gh` (registered by the real `plugins/gh.py` on
        disk) never leaks into other test modules sharing the pytest session
        (same pattern as test_rtk.py)."""
        from hivepilot.registry import RUNNER_MAP

        snapshot = dict(RUNNER_MAP)
        yield
        RUNNER_MAP.clear()
        RUNNER_MAP.update(snapshot)

    def test_plugin_manager_registers_gh_into_runner_map_when_binary_present(
        self, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "gh_enabled", True, raising=False)

        import shutil

        if shutil.which("gh") is None:
            pytest.skip("gh CLI not installed on this host — cannot exercise real discovery path")

        plugins_mod.PluginManager()

        assert "gh" in RUNNER_MAP

        from hivepilot.registry import resolve_runner_class

        assert resolve_runner_class("gh") is RUNNER_MAP["gh"]

    def test_plugin_manager_skips_gh_when_disabled(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "gh_enabled", False, raising=False)
        RUNNER_MAP.pop("gh", None)  # clean baseline (fixture restores after)

        plugins_mod.PluginManager()

        assert "gh" not in RUNNER_MAP
