"""
Tests for the `headroom` plugin (Sprint 3 of the plugins plan).

`plugins/headroom.py` is a local-file plugin (see docs/v4/PLUGINS.md) that
compresses a step's prompt/context before it runs, via the `before_step`
lifecycle hook. It targets `RunnerPayload.metadata["prior_context"]` and
`RunnerPayload.metadata["extra_prompt"]` — both are read verbatim by
`ClaudeRunner._build_prompt` (`hivepilot/runners/claude_runner.py`) off the
SAME `payload` object the orchestrator hands to the hook
(`Orchestrator._execute_task`, `hivepilot/orchestrator.py`,
`self.plugins.run_hook("before_step", payload=payload)`), so an in-place
mutation here is picked up by the runner with no copy in between.

Covers, per the sprint spec:
(a) `register()` exposes a `before_step` hook.
(b) With `headroom.compress` mocked to return a shorter string,
    `before_step(payload=...)` mutates the compressible field(s) in place
    and logs a compression ratio.
(c) Lib absent (`compress is None`): `before_step` is a silent no-op — the
    field is left unchanged, no exception propagates.
(d) An internal error (mocked `compress` raises) is swallowed — `before_step`
    never propagates.
(e) Loading via the real `PluginManager` local-file discovery mechanism
    (mirrors `tests/test_rtk.py` / `tests/test_plugin_obsidian.py`) registers
    the `before_step` hook into `pm.hooks`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import ProjectConfig, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
HEADROOM_PLUGIN_PATH = REPO_ROOT / "plugins" / "headroom.py"


def _load_headroom_module() -> ModuleType:
    """Load plugins/headroom.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_headroom_test", HEADROOM_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def headroom_module() -> ModuleType:
    return _load_headroom_module()


def _payload(tmp_path: Path, **metadata: object) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata=dict(metadata),
        secrets={},
    )


class TestRegister:
    def test_register_exposes_before_step_hook(self, headroom_module: ModuleType) -> None:
        hooks = headroom_module.register()
        assert hooks["before_step"] is headroom_module.before_step


class TestBeforeStepCompressesInPlace:
    def test_compresses_prior_context_and_logs_ratio(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        original = "x" * 1000
        payload = _payload(tmp_path, prior_context=original)

        with (
            patch.object(headroom_module, "compress", return_value="x" * 100) as mock_compress,
            patch.object(headroom_module, "logger", MagicMock()) as mock_logger,
        ):
            headroom_module.before_step(payload=payload)

        assert mock_compress.called
        assert payload.metadata["prior_context"] == "x" * 100
        assert mock_logger.info.called
        call_kwargs = mock_logger.info.call_args.kwargs
        assert call_kwargs["chars_before"] == 1000
        assert call_kwargs["chars_after"] == 100

    def test_compresses_extra_prompt_too(self, headroom_module: ModuleType, tmp_path: Path) -> None:
        payload = _payload(tmp_path, extra_prompt="y" * 500)

        with patch.object(headroom_module, "compress", return_value="y" * 50):
            headroom_module.before_step(payload=payload)

        assert payload.metadata["extra_prompt"] == "y" * 50

    def test_no_compressible_fields_is_a_noop(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path)

        with patch.object(headroom_module, "compress", return_value="unused") as mock_compress:
            headroom_module.before_step(payload=payload)

        assert not mock_compress.called
        assert payload.metadata == {}


class TestLibAbsentIsNoop:
    def test_compress_none_leaves_field_unchanged(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, prior_context="unchanged")

        with patch.object(headroom_module, "compress", None):
            headroom_module.before_step(payload=payload)  # must not raise

        assert payload.metadata["prior_context"] == "unchanged"

    def test_missing_payload_kwarg_is_a_noop(self, headroom_module: ModuleType) -> None:
        with patch.object(headroom_module, "compress", return_value="whatever"):
            headroom_module.before_step()  # must not raise, keyword-tolerant


class TestInternalErrorIsSwallowed:
    def test_compress_raising_does_not_propagate(
        self, headroom_module: ModuleType, tmp_path: Path
    ) -> None:
        payload = _payload(tmp_path, prior_context="some context")

        def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("headroom internal failure")

        with (
            patch.object(headroom_module, "compress", side_effect=_boom),
            patch.object(headroom_module, "logger", MagicMock()) as mock_logger,
        ):
            headroom_module.before_step(payload=payload)  # must not raise

        assert payload.metadata["prior_context"] == "some context"
        assert mock_logger.warning.called


class TestPluginManagerDiscoversHeadroom:
    def test_plugin_manager_registers_before_step_hook(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert any(
            getattr(hook, "__module__", "").startswith("hivepilot_plugin_headroom")
            for hook in pm.hooks.get("before_step", [])
        )
        assert any(r.source == "local-file" and r.name == "headroom" for r in pm.loaded)
