"""
Tests for the `rtk` runner plugin (Sprint 1 of the plugins plan).

`plugins/rtk.py` is a local-file plugin (see docs/v4/PLUGINS.md) that wraps
whatever command a shell-generic step would normally run with `rtk proxy
<cmd>` to cut token usage on command output, and falls back to running the
raw command (no crash) when `rtk` isn't on PATH.

Covers:
(a) `register()` exposes runner kind `rtk` and it does not collide with a
    built-in kind (`KNOWN_RUNNER_KINDS`).
(b) `run()` invokes `rtk proxy <cmd>` when `rtk` is on PATH — mocked
    subprocess, mirroring `tests/test_runner_invocation.py`.
(c) Fallback path: when `shutil.which("rtk")` is None, the raw command runs
    (no `rtk proxy` wrapping) and no exception propagates.
(d) Loading via the real `PluginManager` local-file discovery mechanism
    (mirrors `tests/test_plugin_loading_mechanisms.py` /
    `tests/fixtures/entry_point_plugin.py`) registers `rtk` into
    `hivepilot.registry.RUNNER_MAP`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
RTK_PLUGIN_PATH = REPO_ROOT / "plugins" / "rtk.py"


def _load_rtk_module() -> ModuleType:
    """Load plugins/rtk.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_rtk_test", RTK_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rtk_module() -> ModuleType:
    return _load_rtk_module()


def _payload(tmp_path: Path, command: str = "echo {project_name}") -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="rtk", command=command),
        metadata={},
        secrets={},
    )


class TestRegister:
    def test_register_exposes_rtk_kind(self, rtk_module: ModuleType) -> None:
        hooks = rtk_module.register()
        assert "runners" in hooks
        assert "rtk" in hooks["runners"]

    def test_rtk_kind_does_not_collide_with_a_built_in(self) -> None:
        assert "rtk" not in KNOWN_RUNNER_KINDS

    def test_register_exposes_health_check(self, rtk_module: ModuleType) -> None:
        hooks = rtk_module.register()
        assert "health" in hooks
        assert "rtk" in hooks["health"]
        assert hooks["health"]["rtk"] is rtk_module.health


class TestHealth:
    """Sprint 2 (plugin-health): `health()` reflects `shutil.which("rtk")`."""

    def test_ok_when_rtk_on_path(self, rtk_module: ModuleType) -> None:
        with patch.object(rtk_module.shutil, "which", return_value="/usr/local/bin/rtk"):
            result = rtk_module.health()
        assert result.status == "ok"

    def test_degraded_when_rtk_not_on_path(self, rtk_module: ModuleType) -> None:
        with patch.object(rtk_module.shutil, "which", return_value=None):
            result = rtk_module.health()
        assert result.status == "degraded"
        assert "not on PATH" in result.detail
        assert "raw execution" in result.detail

    def test_health_is_keyword_tolerant(self, rtk_module: ModuleType) -> None:
        """health(**kwargs) must accept (and ignore) arbitrary kwargs — the
        collector (`PluginManager.run_health_check`) calls it with none, but
        the contract is keyword-tolerant per the plugin-health spec."""
        with patch.object(rtk_module.shutil, "which", return_value=None):
            result = rtk_module.health(project="anything")
        assert result.status == "degraded"


class TestRunWrapsWithRtkProxy:
    def test_run_invokes_rtk_proxy_when_rtk_on_path(
        self, rtk_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = rtk_module.RtkRunner(RunnerDefinition(name="rtk", kind="rtk"), settings)
        with (
            patch.object(rtk_module.shutil, "which", return_value="/usr/local/bin/rtk"),
            patch.object(rtk_module.subprocess, "run") as mock_run,
        ):
            runner.run(_payload(tmp_path))

        args = mock_run.call_args.args[0]
        assert args[0] == "rtk"
        assert args[1] == "proxy"
        assert args[2:5] == ["bash", "-lc", "echo proj"]
        assert mock_run.call_args.kwargs["check"] is True

    def test_run_renders_step_command_template(
        self, rtk_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = rtk_module.RtkRunner(RunnerDefinition(name="rtk", kind="rtk"), settings)
        with (
            patch.object(rtk_module.shutil, "which", return_value="/usr/local/bin/rtk"),
            patch.object(rtk_module.subprocess, "run") as mock_run,
        ):
            runner.run(_payload(tmp_path, command="echo {task_name}-{step_name}"))

        args = mock_run.call_args.args[0]
        assert args[-1] == "echo t-s"

    def test_missing_command_raises_value_error(
        self, rtk_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = rtk_module.RtkRunner(RunnerDefinition(name="rtk", kind="rtk"), settings)
        payload = _payload(tmp_path, command="")
        payload.step.command = None
        with pytest.raises(ValueError):
            runner.run(payload)


class TestFallbackWithoutRtkOnPath:
    def test_falls_back_to_raw_command_when_rtk_missing(
        self, rtk_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = rtk_module.RtkRunner(RunnerDefinition(name="rtk", kind="rtk"), settings)
        with (
            patch.object(rtk_module.shutil, "which", return_value=None),
            patch.object(rtk_module.subprocess, "run") as mock_run,
        ):
            runner.run(_payload(tmp_path))  # must not raise

        args = mock_run.call_args.args[0]
        assert args == ["bash", "-lc", "echo proj"]
        assert "rtk" not in args

    def test_fallback_logs_at_info(self, rtk_module: ModuleType, tmp_path: Path) -> None:
        # Missing rtk is expected graceful degradation, not a problem, so it
        # logs at INFO (not WARNING) to avoid flooding logs on every step in
        # environments that intentionally run without rtk installed.
        runner = rtk_module.RtkRunner(RunnerDefinition(name="rtk", kind="rtk"), settings)
        with (
            patch.object(rtk_module.shutil, "which", return_value=None),
            patch.object(rtk_module.subprocess, "run"),
            patch.object(rtk_module, "logger", MagicMock()) as mock_logger,
        ):
            runner.run(_payload(tmp_path))

        assert mock_logger.info.called
        assert not mock_logger.warning.called


class TestPluginManagerDiscoversRtk:
    @pytest.fixture(autouse=True)
    def _restore_runner_map(self):
        """RUNNER_MAP is process-global mutable state — snapshot/restore around
        every test here so `rtk` (registered by the real `plugins/rtk.py` on
        disk) never leaks into other test modules sharing the pytest session
        (same pattern as test_plugin_loading_mechanisms.py)."""
        from hivepilot.registry import RUNNER_MAP

        snapshot = dict(RUNNER_MAP)
        yield
        RUNNER_MAP.clear()
        RUNNER_MAP.update(snapshot)

    def test_plugin_manager_registers_rtk_into_runner_map(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "rtk" in RUNNER_MAP
        assert any(r.source == "local-file" and r.name == "rtk" for r in pm.loaded)
